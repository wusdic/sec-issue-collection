-- 国内行业安全事件库 建库 DDL v0.1(PostgreSQL 16 + pgvector)
-- 依据: design/详细设计.md 第 3 节; 事件 payload 校验依据 schema/event.schema.json

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============ M9 用户与审计 ============

CREATE TABLE app_user (
  id            BIGSERIAL PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE,
  display_name  TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('admin','analyst','reviewer','editor','readonly')),
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
  id         BIGSERIAL PRIMARY KEY,
  user_id    BIGINT REFERENCES app_user(id),
  action     TEXT NOT NULL,             -- e.g. event.publish / source.promote
  target     TEXT NOT NULL,             -- 资源标识
  detail     JSONB,
  at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON audit_log (target, at DESC);

-- ============ M9 词表版本(dictionaries.yaml 发布制) ============

CREATE TABLE dictionary_release (
  id           BIGSERIAL PRIMARY KEY,
  version      TEXT NOT NULL UNIQUE,    -- 与 yaml 中 version 一致
  content      JSONB NOT NULL,          -- 整包词表
  released_by  BIGINT REFERENCES app_user(id),
  released_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  note         TEXT
);

-- ============ M1/M10 源注册表 ============

CREATE TABLE source (
  id              BIGSERIAL PRIMARY KEY,
  name            TEXT NOT NULL,
  identity_key    TEXT UNIQUE,          -- 网站=注册域名(eTLD+1) / 公众号=账号标识; 源发现归一键
  discovery_score REAL,                 -- 候选期评分(日更), 转正后置空
  homepage        TEXT,
  entry_url       TEXT,                 -- 列表页/接口入口(查询型可空)
  kind            TEXT NOT NULL CHECK (kind IN ('page','query')),
  adapter         TEXT NOT NULL,        -- 适配器类名, 如 cac_gov / sogou_wechat
  adapter_config  JSONB NOT NULL DEFAULT '{}',   -- 解析模板/查询参数
  credibility     TEXT NOT NULL CHECK (credibility IN ('S1','S2','S3','S4')),
  tier            TEXT NOT NULL DEFAULT 'B' CHECK (tier IN ('A','B','C')),  -- A:2-4h B:日 C:周
  lifecycle       TEXT NOT NULL DEFAULT 'candidate'
                  CHECK (lifecycle IN ('candidate','trial','active','degraded','retired')),
  discovered_from TEXT,                 -- 人工/定题搜索/引用挖掘
  manual_assist   BOOLEAN NOT NULL DEFAULT FALSE,  -- 强反爬源: 半自动模式
  -- 贡献度(ops 任务日更)
  stat_docs_total    INT NOT NULL DEFAULT 0,
  stat_firsthand     INT NOT NULL DEFAULT 0,  -- 首发文档数
  stat_events_linked INT NOT NULL DEFAULT 0,
  trial_started_at   TIMESTAMPTZ,
  last_success_at    TIMESTAMPTZ,
  fail_streak        INT NOT NULL DEFAULT 0,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (adapter, entry_url)
);

CREATE TABLE source_metric_daily (
  source_id  BIGINT NOT NULL REFERENCES source(id),
  day        DATE NOT NULL,
  fetched    INT NOT NULL DEFAULT 0,
  new_docs   INT NOT NULL DEFAULT 0,
  firsthand  INT NOT NULL DEFAULT 0,
  failures   INT NOT NULL DEFAULT 0,
  PRIMARY KEY (source_id, day)
);

-- ============ M2 抓取执行 ============

CREATE TABLE crawl_run (
  id          BIGSERIAL PRIMARY KEY,
  source_id   BIGINT NOT NULL REFERENCES source(id),
  keyword_run_id BIGINT,                -- 查询型: 关联 keyword_run
  started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  status      TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','ok','partial','failed')),
  urls_found  INT NOT NULL DEFAULT 0,
  urls_new    INT NOT NULL DEFAULT 0,
  error       TEXT
);
CREATE INDEX ON crawl_run (source_id, started_at DESC);

-- ============ M11 原文存档 ============

CREATE TABLE archive_manifest (
  snapshot_id   TEXT PRIMARY KEY,       -- archive/{YYYY}/{MM}/{snapshot_id}/
  status        TEXT NOT NULL CHECK (status IN ('L-A','L-B','L-C','L-D')),
  captured_at   TIMESTAMPTZ NOT NULL,
  final_url     TEXT NOT NULL,
  storage_path  TEXT NOT NULL,          -- MinIO 前缀
  has_full_text BOOLEAN NOT NULL DEFAULT FALSE,
  image_count      INT NOT NULL DEFAULT 0,
  attachment_count INT NOT NULL DEFAULT 0,
  screenshot_pages INT NOT NULL DEFAULT 0,
  manifest_sha256  TEXT,
  fail_reason      TEXT,                -- L-D 必填
  last_verified_at TIMESTAMPTZ,         -- 月度抽检
  verify_ok        BOOLEAN
);

-- ============ M2/M3 原始文档与同稿簇 ============

CREATE TABLE doc_cluster (
  id                 BIGSERIAL PRIMARY KEY,
  primary_doc_id     BIGINT,            -- 首发文档(回填)
  member_count       INT NOT NULL DEFAULT 1,
  first_published_at TIMESTAMPTZ
);

CREATE TABLE raw_document (
  id             BIGSERIAL PRIMARY KEY,
  source_id      BIGINT NOT NULL REFERENCES source(id),
  crawl_run_id   BIGINT REFERENCES crawl_run(id),
  url            TEXT NOT NULL,
  url_normalized TEXT NOT NULL UNIQUE,  -- 10.1 URL 层去重
  final_url      TEXT,
  title          TEXT,
  publisher      TEXT,
  published_at   TIMESTAMPTZ,
  fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  http_status    INT,
  content_text   TEXT,                  -- 提取后正文
  text_tsv       tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(content_text,''))) STORED,
  simhash        BIGINT,                -- 10.2 文档层去重
  cluster_id     BIGINT REFERENCES doc_cluster(id),
  is_primary     BOOLEAN NOT NULL DEFAULT TRUE,   -- 是否同稿簇首发
  snapshot_id    TEXT REFERENCES archive_manifest(snapshot_id),
  screen_status  TEXT NOT NULL DEFAULT 'pending'
                 CHECK (screen_status IN ('pending','manual_queue','screened_in','screened_out')),
  screen_score   REAL,
  screen_reason  TEXT,
  seen_again     INT NOT NULL DEFAULT 0  -- URL 重复命中计数(热度)
);
CREATE INDEX ON raw_document (screen_status, fetched_at DESC);
CREATE INDEX ON raw_document (cluster_id);
CREATE INDEX ON raw_document USING gin (text_tsv);
CREATE INDEX ON raw_document (simhash);

-- ============ M10 源发现(详细设计第 8 节) ============

CREATE TABLE source_discovery_evidence (
  id           BIGSERIAL PRIMARY KEY,
  identity_key TEXT NOT NULL,           -- 域名 eTLD+1 或 公众号标识
  display_name TEXT,                    -- 站点名/公众号名(最近一次观察值)
  kind_guess   TEXT CHECK (kind_guess IN ('website','wechat_mp','forum','other')),
  channel      TEXT NOT NULL CHECK (channel IN
               ('event_search',        -- D1 事件检索伴生
                'citation',            -- D2 引文/转载/首发溯源
                'wechat_reference',    -- D3 公众号互推/白名单
                'directory',           -- D4 导航与聚合站
                'source_search',       -- D5 找源专用检索
                'manual')),            -- D6 人工推荐
  evidence_doc_id BIGINT REFERENCES raw_document(id),
  evidence_url TEXT,
  was_cluster_primary BOOLEAN NOT NULL DEFAULT FALSE,  -- 曾为同稿簇首发(高权重)
  first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
  hit_count    INT NOT NULL DEFAULT 1
);
CREATE INDEX ON source_discovery_evidence (identity_key);
CREATE INDEX ON source_discovery_evidence (channel, last_seen DESC);

CREATE TABLE source_blacklist (
  identity_key TEXT PRIMARY KEY,
  reason       TEXT NOT NULL,           -- 营销号/纯搬运/内容农场/违规站点
  by_user      BIGINT REFERENCES app_user(id),
  at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ M4/M5 事件 ============

CREATE TABLE event (
  event_id     TEXT PRIMARY KEY,        -- SEC-YYYYMMDD-NNNN
  payload      JSONB NOT NULL,          -- 通过 event.schema.json 校验(应用层)
  -- 高频查询列: 发布/更新触发器从 payload 同步
  status       TEXT NOT NULL DEFAULT 'draft'
               CHECK (status IN ('draft','published','monitoring','closed')),
  occurred_date DATE,
  disclosed_date DATE,
  industry_l1  TEXT,
  industry_l2  TEXT,
  province     TEXT,
  city         TEXT,
  org_name     TEXT,
  org_uscc     TEXT,
  org_type     TEXT,
  org_size     TEXT,
  severity     TEXT,
  attack_types TEXT[] NOT NULL DEFAULT '{}',
  consequences TEXT[] NOT NULL DEFAULT '{}',
  confidence_overall TEXT,
  completeness_score REAL,
  dict_version TEXT REFERENCES dictionary_release(version),  -- 录入时词表版本
  embedding    vector(1024),            -- 事件摘要向量(10.3 语义召回)
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  first_published_at TIMESTAMPTZ
);
CREATE INDEX ON event (status, disclosed_date DESC);
CREATE INDEX ON event (industry_l1, occurred_date DESC);
CREATE INDEX ON event (org_uscc) WHERE org_uscc IS NOT NULL;
CREATE INDEX ON event USING gin (attack_types);
CREATE INDEX ON event USING gin (payload jsonb_path_ops);
CREATE INDEX ON event USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON event USING gin (org_name gin_trgm_ops);

CREATE TABLE event_source (
  event_id    TEXT NOT NULL REFERENCES event(event_id) ON DELETE CASCADE,
  ref_id      TEXT NOT NULL,            -- payload.sources[].ref_id
  doc_id      BIGINT REFERENCES raw_document(id),
  snapshot_id TEXT REFERENCES archive_manifest(snapshot_id),
  credibility TEXT NOT NULL CHECK (credibility IN ('S1','S2','S3','S4')),
  supports_fields TEXT[] NOT NULL DEFAULT '{}',
  PRIMARY KEY (event_id, ref_id)
);

CREATE TABLE event_change_log (
  id        BIGSERIAL PRIMARY KEY,
  event_id  TEXT NOT NULL REFERENCES event(event_id) ON DELETE CASCADE,
  at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  by_user   BIGINT REFERENCES app_user(id),
  field     TEXT NOT NULL,
  old_value JSONB,
  new_value JSONB,
  source_ref TEXT
);
CREATE INDEX ON event_change_log (event_id, at DESC);

-- 发布硬校验(方案 5/6/11 的库级兜底):
--   confirmed 金额通道非空 ⇒ 必须存在 S1/S2 关联来源
CREATE OR REPLACE FUNCTION check_event_publish() RETURNS trigger AS $$
DECLARE
  has_confirmed BOOLEAN;
  has_s12       BOOLEAN;
BEGIN
  IF NEW.status IN ('published','monitoring','closed') THEN
    SELECT EXISTS (
      SELECT 1 FROM jsonb_each(NEW.payload) AS t(k, v)
      WHERE k LIKE 'loss_%'
        AND v -> 'confirmed_cny' IS NOT NULL
        AND v -> 'confirmed_cny' <> 'null'::jsonb
    ) INTO has_confirmed;
    IF has_confirmed THEN
      SELECT EXISTS (
        SELECT 1 FROM event_source es
        WHERE es.event_id = NEW.event_id AND es.credibility IN ('S1','S2')
      ) INTO has_s12;
      IF NOT has_s12 THEN
        RAISE EXCEPTION 'event % has confirmed loss but no S1/S2 source', NEW.event_id;
      END IF;
    END IF;
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_event_publish BEFORE INSERT OR UPDATE ON event
FOR EACH ROW EXECUTE FUNCTION check_event_publish();

-- ============ M5 复核流转 ============

CREATE TABLE review_task (
  id         BIGSERIAL PRIMARY KEY,
  event_id   TEXT NOT NULL REFERENCES event(event_id) ON DELETE CASCADE,
  stage      TEXT NOT NULL DEFAULT 'extracted'
             CHECK (stage IN ('extracted','first_review','second_review','published','rejected')),
  needs_double BOOLEAN NOT NULL DEFAULT FALSE,   -- 含 confirmed 金额 ⇒ TRUE
  assignee   BIGINT REFERENCES app_user(id),
  first_reviewer  BIGINT REFERENCES app_user(id),
  second_reviewer BIGINT REFERENCES app_user(id),
  comments   JSONB NOT NULL DEFAULT '[]',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON review_task (stage, updated_at);

-- ============ M6 回访 ============

CREATE TABLE followup_task (
  id         BIGSERIAL PRIMARY KEY,
  event_id   TEXT NOT NULL REFERENCES event(event_id) ON DELETE CASCADE,
  kind       TEXT NOT NULL CHECK (kind IN ('T30','T90','T180','T365','manual')),
  due_date   DATE NOT NULL,
  reason     TEXT,                      -- 触发条件: 金额未落地/立案未处罚/诉讼中/采购未知
  status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','skipped')),
  search_pack JSONB,                    -- 一键检索包(生成的查询链接)
  findings   TEXT,
  done_by    BIGINT REFERENCES app_user(id),
  done_at    TIMESTAMPTZ
);
CREATE INDEX ON followup_task (status, due_date);

-- ============ M2/M10 关键词矩阵与查询执行 ============

CREATE TABLE keyword_set (
  id          BIGSERIAL PRIMARY KEY,
  version     TEXT NOT NULL UNIQUE,
  content     JSONB NOT NULL,           -- config/keyword_matrix.yaml 整包
  is_active   BOOLEAN NOT NULL DEFAULT FALSE,
  published_at TIMESTAMPTZ
);

CREATE TABLE keyword_run (
  id          BIGSERIAL PRIMARY KEY,
  keyword_set_id BIGINT REFERENCES keyword_set(id),   -- B5/B6 等非矩阵搜索可空
  source_id   BIGINT NOT NULL REFERENCES source(id),
  behavior    TEXT NOT NULL DEFAULT 'B1'
              CHECK (behavior IN ('B1','B2','B3','B4','B5','B6','B7','B8')),  -- 搜索行为分类(见搜索行为规范)
  watch_target_id BIGINT,                             -- B5: 归因到监控对象
  query       TEXT NOT NULL,
  ran_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  pages_fetched INT NOT NULL DEFAULT 1,
  truncated   BOOLEAN NOT NULL DEFAULT FALSE,         -- C2: 命中饱和被截断(禁止无声截断)
  results     INT NOT NULL DEFAULT 0,
  new_docs    INT NOT NULL DEFAULT 0,
  new_source_candidates INT NOT NULL DEFAULT 0,
  result_snapshot JSONB                               -- C10: 结果清单快照(可回放审计)
);
CREATE INDEX ON keyword_run (behavior, ran_at DESC);

-- 搜索行为规范 B5: 监控名单(重点单位/涉事产品/攻击组织/专题的持续盯防)
CREATE TABLE watch_target (
  id          BIGSERIAL PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('org','product','attacker_group','topic')),
  value       TEXT NOT NULL,
  aliases     TEXT[] NOT NULL DEFAULT '{}',           -- 简称/英文名/曾用名, 查询时展开
  reason      TEXT,                                   -- 入名单原因
  linked_event_id TEXT REFERENCES event(event_id),    -- 由哪个事件带入(可空: 人工主动布防)
  tier        TEXT NOT NULL DEFAULT 'B' CHECK (tier IN ('A','B','C')),  -- 搜索频率
  active      BOOLEAN NOT NULL DEFAULT TRUE,
  expires_at  DATE,                                   -- 到期自动停(可续)
  created_by  BIGINT REFERENCES app_user(id),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (kind, value)
);

-- 搜索能力 C4: 水位线增量(每源×查询只处理新增)
CREATE TABLE search_watermark (
  source_id   BIGINT NOT NULL REFERENCES source(id),
  query_hash  TEXT NOT NULL,                          -- 规范化查询串哈希
  last_ran_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (source_id, query_hash)
);

-- ============ M10 覆盖对标 ============

CREATE TABLE benchmark_batch (
  id         BIGSERIAL PRIMARY KEY,
  name       TEXT NOT NULL,             -- 如 2026-07 CNCERT 月报
  period     TEXT NOT NULL,             -- YYYY-MM
  source_desc TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE benchmark_item (
  id         BIGSERIAL PRIMARY KEY,
  batch_id   BIGINT NOT NULL REFERENCES benchmark_batch(id) ON DELETE CASCADE,
  summary    TEXT NOT NULL,
  matched_event_id TEXT REFERENCES event(event_id),
  is_missed  BOOLEAN,
  miss_reason TEXT CHECK (miss_reason IN ('缺源','关键词盲区','粗筛误杀','范围外','其他') OR miss_reason IS NULL)
);

-- ============ M8 线索 ============

CREATE TABLE lead (
  id           BIGSERIAL PRIMARY KEY,
  event_id     TEXT NOT NULL REFERENCES event(event_id),
  target_org   TEXT NOT NULL,           -- 事件单位本身或同款预警/同行扩展单位
  target_kind  TEXT NOT NULL CHECK (target_kind IN ('victim','same_product','peer')),
  score        REAL NOT NULL,
  window_stage TEXT NOT NULL CHECK (window_stage IN ('应急期','整改期','预算期','已过窗')),
  products     TEXT[] NOT NULL DEFAULT '{}',
  talk_track   TEXT,                    -- 话术依据(引用公开事实)
  status       TEXT NOT NULL DEFAULT 'new'
               CHECK (status IN ('new','dispatched','followed','opportunity','won','lost','dropped')),
  dispatched_at TIMESTAMPTZ,
  feedback     JSONB,                   -- CRM 回流
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (event_id, target_org)
);
CREATE INDEX ON lead (status, score DESC);
