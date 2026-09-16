-- =====================================================================
-- 全国环评「报告 + 审批意见」采集库   schema v0.4   PostgreSQL 14+
--
-- 四层，方向单一：
--   R 登记层  去哪里爬      region / authority / site / entry / entry_scope
--   O 观察层  看到了什么    crawl_run / notice / notice_item / attachment / file_blob   （只追加，不改写原值）
--   I 推断层  怎么关联      eia_case / case_item_link / case_document                   （带证据，可整体重算）
--   D 交付层  两件套合同    case_delivery / case_gap / review_task
--
-- 业务口径（2026-09-15 确认）：
--   1. 报告取用：审批阶段优先（批复公告随附 → 拟审批公示随附，含受理和拟审批合并公示）→ 受理公示版兜底；
--      同阶段报批稿优先；兜底时记录本事项的拟审批公示是否已经抓到，没抓到的回溯补抓，抓到更好的版本自动替换
--   2. 两份文件都必须来自官方网站：环保局网站、区县环保局在政府门户上的栏目、行政审批局 / 管委会 / 政务服务网站都算；
--      建设单位全本公示、第三方平台、网盘都不取
--   3. 辐射类（含输变电等电磁类）、告知承诺制、不予批准不在范围
--   4. 关联分 0.6 – 0.9 的也交付，带「待复核」标记
--   5. 时间范围：批复日 ≥ 2021-01-01；网站翻不到的，回溯到最早可达日期
--
-- 标记 @pg-only 的块在 SQLite 冒烟测试里会被替换，其余语句两边通用。
-- =====================================================================

-- @pg-only-begin
CREATE SCHEMA IF NOT EXISTS eia;
SET search_path TO eia;
-- @pg-only-end

-- ---------------------------------------------------------------------
-- 配置与字典：业务口径改这里，不改代码
-- ---------------------------------------------------------------------
CREATE TABLE app_config (                      -- 单行配置
  id                      smallint PRIMARY KEY CHECK (id = 1),
  scope_start_date        date NOT NULL,       -- 批复日早于它的事项不在范围
  report_lookback_months  int NOT NULL,        -- 报告类栏目比批复类栏目多往前回溯的月数
  complete_min_score      numeric(4,3) NOT NULL,  -- COMPLETE 门槛；只能比表约束 0.9 更严
  deliver_min_score       numeric(4,3) NOT NULL,  -- 带标记交付门槛；只能比表约束 0.6 更严
  note                    text
);
INSERT INTO app_config (id, scope_start_date, report_lookback_months, complete_min_score, deliver_min_score, note)
VALUES (1, '2021-01-01', 6, 0.9, 0.6, '苏州 102 对报告→批复间隔：中位 14 天，最长 75 天；回溯多留到 6 个月');

CREATE TABLE dict_stage (
  code             text PRIMARY KEY,
  name_cn          text NOT NULL,
  report_rank      smallint,                  -- 报告取用优先级，越小越优先；NULL = 不作为报告来源
  approval_source  boolean NOT NULL DEFAULT false,  -- 该阶段公告里的文件可作为「审批意见」
  note             text
);
INSERT INTO dict_stage (code, name_cn, report_rank, approval_source, note) VALUES
  ('APPROVAL',         '审批决定公告 / 批复公告', 1,    true,  '批复公告随附的报告优先（已确认）'),
  ('PRE_APPROVAL',     '拟审批公示 / 审批前公示', 2,    false, '拟审批公示随附的报告'),
  ('ACCEPT_PRE_APPROVAL', '受理和拟审批合并公示', 2,  false, '合并公示按拟审批阶段排序，不算兜底'),
  ('ACCEPT',           '受理公示',               3,    false, '兜底：受理公示版报告'),
  ('BUILDER_FULLTEXT', '建设单位报批前全本公示',   NULL, false, '不取（已确认：必须来自官方网站）'),
  ('OTHER',            '其他 / 未识别',           NULL, false, NULL);

CREATE TABLE dict_report_version (             -- 报告版本：附件名 / 公告标题 / PDF 首页里的字样，同一阶段内排序用
  code          text PRIMARY KEY,
  name_cn       text NOT NULL,
  pattern       text,                        -- 由程序按正则匹配
  version_rank  smallint NOT NULL,           -- 越小越接近最终版
  note          text
);
INSERT INTO dict_report_version (code, name_cn, pattern, version_rank, note) VALUES
  ('SUBMITTED_FOR_APPROVAL', '报批稿',              '报批|审批稿|审批版', 1, '技术评估修改后报审批的版本'),
  ('TECH_REVIEW',            '送审稿',              '送审',              2, '提交技术评估的版本'),
  ('PUBLIC_DRAFT',           '公示稿 / 征求意见稿',  '公示|征求意见|全本', 3, '公开用的版本，可能有删减'),
  ('UNKNOWN',                '未标版本',            NULL,                4, NULL);

CREATE TABLE dict_source_class (               -- 网站来源等级：决定文件能不能交付
  code         text PRIMARY KEY,
  name_cn      text NOT NULL,
  deliverable  boolean NOT NULL,
  note         text
);
INSERT INTO dict_source_class (code, name_cn, deliverable, note) VALUES
  ('EEB_OWN_SITE',        '生态环境部门自有网站',               true,  '如 sthjj.suzhou.gov.cn'),
  ('EEB_PORTAL_COLUMN',   '生态环境部门在政府门户上的官方栏目',   true,  '区县生态环境局没有独立域名、挂在区政府网站上的专栏（已确认可交付）'),
  ('OTHER_APPROVER_SITE', '行政审批局 / 管委会 / 政务服务的网站或门户公示栏目', true,  '审批机关的官方网站，含政府门户上的公示栏目（已确认可交付）'),
  ('THIRD_PARTY',         '第三方平台 / 建设单位 / 网盘',        false, '只作线索，不交付');

CREATE TABLE dict_scope_rule (                 -- 范围判定规则；由程序按正则匹配，命中后写 notice_item 的标记
  rule_code     text PRIMARY KEY,
  applies_to    text NOT NULL CHECK (applies_to IN ('title','project_name','eia_category','entry_name')),
  pattern       text NOT NULL,
  scope_reason  text NOT NULL CHECK (scope_reason IN ('OUT_RADIATION','OUT_COMMITMENT','OUT_REJECTED')),
  enabled       boolean NOT NULL DEFAULT true,
  note          text
);
INSERT INTO dict_scope_rule (rule_code, applies_to, pattern, scope_reason, enabled, note) VALUES
  ('RAD_NUCLEAR',    'project_name', '核技术利用|放射性|射线装置|同位素|加速器|伽马刀|DSA|PET',  'OUT_RADIATION',  true, '核技术利用'),
  ('RAD_POWER_GRID', 'project_name', '输变电|变电站|换流站|输电线路|开关站',                     'OUT_RADIATION',  true, '分类管理名录「核与辐射」大类含输变电（已确认算辐射类）'),
  ('RAD_EM_OTHER',   'project_name', '广播电视发射|通信基站|雷达站|卫星地球站',                   'OUT_RADIATION',  true, '其他电磁类'),
  ('RAD_ENTRY',      'entry_name',   '辐射',                                                   'OUT_RADIATION',  true, '辐射项目专栏'),
  ('RAD_TITLE',      'title',        '辐射',                                                   'OUT_RADIATION',  true, NULL),
  ('COMMITMENT',     'title',        '告知承诺|承诺制',                                         'OUT_COMMITMENT', true, NULL),
  ('REJECTED',       'title',        '不予批准|不予审批|不予许可|予以退回',                       'OUT_REJECTED',   true, NULL);

CREATE TABLE dict_gap_reason (
  code     text PRIMARY KEY,
  name_cn  text NOT NULL,
  retry    boolean NOT NULL,                  -- 是否由调度器自动复查
  note     text
);
INSERT INTO dict_gap_reason (code, name_cn, retry, note) VALUES
  ('NOT_YET_CRAWLED',     '该机关对应栏目尚未覆盖',                 true,  '默认值，回溯任务跑完后改写'),
  ('OUT_OF_WINDOW',       '网站翻不到该日期，已回溯到最早可达日期',   false, NULL),
  ('NOT_PUBLISHED',       '已查遍机关全部官方入口，确认未公开文件',   true,  '低频复查，evidence_url 必填'),
  ('NOT_OFFICIAL_SOURCE', '只在非官方渠道找到文件',                  true,  '网盘、第三方平台、建设单位'),
  ('FILE_NOT_VERIFIED',   '官方网站上有文件，但下载或校验未通过',      true,  NULL),
  ('LISTED_ONLY',         '只公布名单 / 文号，没有文件',              true,  NULL),
  ('LINK_DEAD',           '链接失效',                             true,  NULL),
  ('UNSUPPORTED',         '格式暂不支持（rar / 加密 / 无文字层图片）',  false, NULL),
  ('PENDING_DECISION',    '尚未作出审批决定',                       true,  NULL);

-- ---------------------------------------------------------------------
-- R 登记层
-- ---------------------------------------------------------------------
CREATE TABLE region (
  region_code  char(6) PRIMARY KEY,           -- GB/T 2260 行政区划代码
  name         text NOT NULL,
  level        text NOT NULL CHECK (level IN ('country','province','city','county')),
  parent_code  char(6) REFERENCES region(region_code)
);

CREATE TABLE authority (                       -- 审批机关 = 发文主体，不是网站
  authority_id        bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  authority_code      text NOT NULL UNIQUE,     -- {region_code}-{简拼}，如 320500-SZSTHJJ
  name                text NOT NULL,            -- 规范全称：苏州市生态环境局
  aliases             text[] NOT NULL DEFAULT '{}',  -- 公告里出现过的其他写法
  uscc                char(18) UNIQUE,          -- 机关统一社会信用代码，能取到就填
  region_code         char(6) NOT NULL REFERENCES region(region_code),
  authority_type      text NOT NULL CHECK (authority_type IN
                        ('MEE','PROVINCE_EEB','CITY_EEB','COUNTY_EEB','ADMIN_APPROVAL','ZONE_COMMITTEE','GOV_SERVICE','OTHER')),
  parent_authority_id bigint REFERENCES authority(authority_id),
  valid_from          date,                     -- 机构改革、审批权划转
  valid_to            date,
  note                text
);

CREATE TABLE site (
  site_id             bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  root_url            text NOT NULL UNIQUE,
  name                text NOT NULL,
  owner_authority_id  bigint REFERENCES authority(authority_id),
  source_class        text NOT NULL REFERENCES dict_source_class(code),  -- 来源等级，决定能否交付
  gov_site_code       text,                     -- 政府网站标识码（页脚可见），有则填
  official_evidence   text,                     -- 判定「官方」的依据：标识码 / ICP 备案主体 / 上级政府网站的入口链接
  file_hosts          text[] NOT NULL DEFAULT '{}',  -- 除站点域名外，允许的附件域名（同一机关的文件服务器）
  cms_family          text,                     -- trs / hanweb / 集约化平台 / custom / api
  min_interval_s      numeric NOT NULL DEFAULT 3,
  requires_login      boolean NOT NULL DEFAULT false,
  note                text,
  CHECK (source_class = 'THIRD_PARTY' OR official_evidence IS NOT NULL)  -- 算官方就必须写依据
);

CREATE TABLE entry (                           -- 爬取入口 = 一个栏目或一个接口。飞书需求表一行对应这里一行
  entry_id         bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  entry_code       text NOT NULL UNIQUE,        -- {region_code}-{站点简拼}-{stage}-{nn}，如 310000-SHSTHJ-APPROVAL-01
  site_id          bigint NOT NULL REFERENCES site(site_id),
  name             text NOT NULL,               -- 栏目路径：环评与许可证 > 审批决定公告
  list_url         text NOT NULL,
  adapter          text NOT NULL,               -- 代码模块名：trs / paged / sipac / shhj_platform
  adapter_params   jsonb NOT NULL DEFAULT '{}', -- link_pattern、翻页模板、接口参数
  stage_hint       text REFERENCES dict_stage(code),
  mixed            boolean NOT NULL DEFAULT false,  -- 栏目混有非环评公告
  scope_hint       text CHECK (scope_hint IN ('OUT_RADIATION','OUT_COMMITMENT')),  -- 辐射、告知承诺专栏：非空即不抓
  status           text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','paused','broken','retired')),
  owner            text,                        -- 负责人
  window_earliest  date,                        -- 列表实际能翻到的最早发布日期
  backfill_status  text NOT NULL DEFAULT 'pending' CHECK (backfill_status IN
                     ('pending','running','reached_target','reached_site_limit')),
  last_success_at  timestamptz,
  list_cursor      jsonb,                       -- 增量游标：最新 pub_date / url
  created_at       timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
  note             text
);

CREATE TABLE entry_scope (                     -- 入口替哪些机关发布（市局栏目发区县分局、区政府栏目发开发区）
  entry_id      bigint NOT NULL REFERENCES entry(entry_id),
  authority_id  bigint NOT NULL REFERENCES authority(authority_id),
  PRIMARY KEY (entry_id, authority_id)
);

-- ---------------------------------------------------------------------
-- O 观察层（只追加；重复看到只更新 last_seen_at）
-- ---------------------------------------------------------------------
CREATE TABLE crawl_run (
  run_id        bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  entry_id      bigint NOT NULL REFERENCES entry(entry_id),
  mode          text NOT NULL CHECK (mode IN ('full','incremental','backsearch','recheck')),
  code_version  text NOT NULL,                  -- git sha
  started_at    timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at   timestamptz,
  pages_read    int NOT NULL DEFAULT 0,
  notices_new   int NOT NULL DEFAULT 0,
  notices_seen  int NOT NULL DEFAULT 0,
  errors        int NOT NULL DEFAULT 0,
  status        text NOT NULL DEFAULT 'running' CHECK (status IN ('running','ok','partial','failed'))
);

CREATE TABLE notice (                          -- 一条公告详情页（或接口里的一条记录）
  notice_id       bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  entry_id        bigint NOT NULL REFERENCES entry(entry_id),      -- 首次发现它的入口
  first_run_id    bigint NOT NULL REFERENCES crawl_run(run_id),
  url             text NOT NULL UNIQUE,         -- 规范化详情页 URL（去跟踪参数）
  title           text NOT NULL,
  pub_date        date,
  publisher_text  text,                          -- 页面上写的发布单位原文
  authority_id    bigint REFERENCES authority(authority_id),       -- 解析后的审批机关
  stage           text NOT NULL DEFAULT 'OTHER' REFERENCES dict_stage(code),
  stage_method    text CHECK (stage_method IN ('title_rule','entry_hint','manual')),
  stage_rule      text,                          -- 命中的规则 id / 原因
  relevance       text NOT NULL DEFAULT 'unknown' CHECK (relevance IN ('eia','excluded','irrelevant','unknown')),
  raw_uri         text,                          -- 原始 HTML/JSON 快照
  raw_sha256      char(64),
  fetch_status    text NOT NULL DEFAULT 'new' CHECK (fetch_status IN ('new','fetched','failed','gone')),
  http_status     smallint,
  first_seen_at   timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen_at    timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
  error           text
);
CREATE INDEX ix_notice_auth_stage_date ON notice (authority_id, stage, pub_date);

CREATE TABLE notice_item (                     -- 公告中的一行项目（一条公告可列 N 个项目）
  item_id            bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  notice_id          bigint NOT NULL REFERENCES notice(notice_id),
  seq                int NOT NULL DEFAULT 1,
  project_name_raw   text NOT NULL,
  project_name_norm  text NOT NULL,
  builder_raw        text,
  builder_norm       text,
  builder_uscc       char(18),                   -- 建设单位统一社会信用代码
  project_code       text,                       -- 投资项目统一代码 2509-320193-89-01-544060
  doc_no_raw         text,
  doc_no_norm        text,                       -- 批复文号规范化：苏环建[2026]85号
  accept_no          text,                       -- 受理 / 办件编号
  eia_form           text NOT NULL DEFAULT 'UNKNOWN' CHECK (eia_form IN ('REPORT_BOOK','REPORT_TABLE','UNKNOWN')),
  eia_category       text,                       -- 名录类别 / 行业类别原文
  is_radiation       boolean NOT NULL DEFAULT false,  -- 命中 dict_scope_rule 的辐射类规则
  approval_mode      text NOT NULL DEFAULT 'UNKNOWN' CHECK (approval_mode IN ('STANDARD','COMMITMENT','UNKNOWN')),
  scope_rules        text,                       -- 命中的 rule_code，逗号分隔
  eia_org            text,
  location           text,
  event_date         date,                       -- 受理日 / 拟审批日 / 批复日
  decision           text NOT NULL DEFAULT 'UNKNOWN' CHECK (decision IN ('APPROVE','REJECT','WITHDRAW','UNKNOWN')),
  norm_version       text NOT NULL,              -- 规范化函数版本，改规则后可重算
  raw                jsonb NOT NULL DEFAULT '{}',
  UNIQUE (notice_id, seq)
);
CREATE INDEX ix_item_project_code ON notice_item (project_code) WHERE project_code IS NOT NULL;
CREATE INDEX ix_item_doc_no       ON notice_item (doc_no_norm)  WHERE doc_no_norm IS NOT NULL;
CREATE INDEX ix_item_builder_name ON notice_item (builder_norm, project_name_norm);

CREATE TABLE file_blob (                       -- 文件内容本身，sha256 去重；同一份报告在受理/拟审批两处出现只存一次
  sha256          char(64) PRIMARY KEY,
  size_bytes      bigint NOT NULL,
  mime            text,
  ext             text,
  storage_uri     text NOT NULL,                 -- oss://{bucket}/eia/blob/ab/ab12….pdf
  pdf_sha256      char(64),                      -- 转换后的 PDF（doc/wps/图片/正文）
  pdf_uri         text,
  pages           int,
  has_text_layer  boolean,
  verified        boolean NOT NULL DEFAULT false, -- 能打开、页数>0、不是错误页
  verified_at     timestamptz,
  first_seen_at   timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE attachment (                      -- 页面上的一个文件链接（同一文件可出现在多处）
  att_id           bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  notice_id        bigint NOT NULL REFERENCES notice(notice_id),
  item_id          bigint REFERENCES notice_item(item_id),   -- 能定位到项目行就填
  parent_att_id    bigint REFERENCES attachment(att_id),     -- 压缩包内文件指向压缩包
  kind             text NOT NULL CHECK (kind IN ('file','body_html','image_set','zip_member','netdisk')),
  url              text NOT NULL,                 -- 绝对地址；正文型为 notice.url + '#body'
  url_host         text,                          -- 文件地址的域名
  on_official_host boolean NOT NULL DEFAULT false, -- url_host 属于站点域名或 site.file_hosts；网盘恒为 false
  anchor_text      text,                          -- 链接文字 / 附件名原文
  ext              text,
  netdisk_code     text,
  doc_role         text NOT NULL DEFAULT 'unknown' CHECK (doc_role IN ('report','approval','participation','other','unknown')),
  version_label    text NOT NULL DEFAULT 'UNKNOWN' REFERENCES dict_report_version(code),  -- 报告版本字样
  role_method      text CHECK (role_method IN ('name_rule','stage_default','content_rule','manual')),
  blob_sha256      char(64) REFERENCES file_blob(sha256),
  fetch_status     text NOT NULL DEFAULT 'new' CHECK (fetch_status IN
                     ('new','ok','http_error','timeout','unsupported','netdisk_manual','gone')),
  http_status      smallint,
  attempts         int NOT NULL DEFAULT 0,
  last_attempt_at  timestamptz,
  error            text,
  UNIQUE (notice_id, url),
  CHECK (kind <> 'netdisk' OR NOT on_official_host)
);
CREATE INDEX ix_att_blob ON attachment (blob_sha256);

-- ---------------------------------------------------------------------
-- I 推断层（全部带证据；规则升级后可删掉 rule 产生的记录重算，人工记录保留）
-- ---------------------------------------------------------------------
CREATE TABLE eia_case (                        -- 环评审批事项：一个项目的一次环评文件审批；重新报批 = 新 case
  case_id               bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  case_code             text NOT NULL UNIQUE,  -- EIA-{region_code}-{yyyy}-{6位流水}，对外引用用它
  authority_id          bigint NOT NULL REFERENCES authority(authority_id),
  region_code           char(6) NOT NULL REFERENCES region(region_code),
  project_name          text NOT NULL,          -- 展示用，取最完整的一版；不做键
  project_name_norm     text NOT NULL,
  builder_name          text,
  builder_norm          text,
  builder_uscc          char(18),
  project_code          text,
  eia_form              text NOT NULL DEFAULT 'UNKNOWN',
  approval_doc_no_norm  text,
  accept_date           date,
  pre_approval_date     date,
  approval_date         date,
  decision              text NOT NULL DEFAULT 'UNKNOWN' CHECK (decision IN ('APPROVE','REJECT','WITHDRAW','UNKNOWN')),
  is_radiation          boolean NOT NULL DEFAULT false,
  approval_mode         text NOT NULL DEFAULT 'STANDARD' CHECK (approval_mode IN ('STANDARD','COMMITMENT')),
  scope                 text NOT NULL DEFAULT 'IN_SCOPE' CHECK (scope IN
                          ('IN_SCOPE','OUT_RADIATION','OUT_COMMITMENT','OUT_REJECTED','OUT_BEFORE_START')),  -- refresh 计算
  lifecycle             text NOT NULL DEFAULT 'SEEN_ACCEPT' CHECK (lifecycle IN ('SEEN_ACCEPT','SEEN_PRE_APPROVAL','DECIDED')),
  merged_into           bigint REFERENCES eia_case(case_id),  -- 合并后指向保留的 case；旧 id 不删
  created_at            timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at            timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uq_case_doc_no ON eia_case (authority_id, approval_doc_no_norm)
  WHERE approval_doc_no_norm IS NOT NULL AND merged_into IS NULL;

CREATE TABLE case_item_link (                  -- 项目行 → 审批事项，一条关联一份证据
  item_id     bigint NOT NULL REFERENCES notice_item(item_id),
  case_id     bigint NOT NULL REFERENCES eia_case(case_id),
  match_key   text NOT NULL CHECK (match_key IN
                ('SEED','SAME_ROW','PROJECT_CODE','DOC_NO','ACCEPT_NO','BUILDER_NAME_EXACT','NAME_FUZZY','MANUAL')),
  score       numeric(4,3) NOT NULL CHECK (score BETWEEN 0 AND 1),
  evidence    jsonb NOT NULL DEFAULT '{}',     -- 命中字段值、相似度、日期差、否决项
  state       text NOT NULL DEFAULT 'candidate' CHECK (state IN ('candidate','accepted','rejected')),
  decided_by  text NOT NULL,                   -- rule:matcher@v3 / reviewer:姓名
  decided_at  timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (item_id, case_id),
  CHECK (state <> 'accepted' OR score >= 0.6)  -- 低于 0.6 的不能挂到事项上
);
CREATE UNIQUE INDEX uq_item_one_accepted_case ON case_item_link (item_id) WHERE state = 'accepted';

CREATE TABLE case_document (                   -- 审批事项的候选文件；selected 由 refresh 逻辑写
  case_id       bigint NOT NULL REFERENCES eia_case(case_id),
  att_id        bigint NOT NULL REFERENCES attachment(att_id),
  role          text NOT NULL CHECK (role IN ('report','approval')),
  source_stage  text NOT NULL REFERENCES dict_stage(code),
  selected      boolean NOT NULL DEFAULT false,
  reason        text,
  PRIMARY KEY (case_id, att_id, role)
);
CREATE UNIQUE INDEX uq_case_one_selected ON case_document (case_id, role) WHERE selected;

-- ---------------------------------------------------------------------
-- D 交付层
-- ---------------------------------------------------------------------
CREATE TABLE case_delivery (                   -- 一个 case 一行；COMPLETE 和 LINK_REVIEW 行交付
  case_id                bigint PRIMARY KEY REFERENCES eia_case(case_id),
  delivery_status        text NOT NULL CHECK (delivery_status IN
                           ('COMPLETE','LINK_REVIEW','NO_LINK_EVIDENCE','MISSING_REPORT','MISSING_APPROVAL',
                            'MISSING_BOTH','PENDING_DECISION','OUT_OF_SCOPE')),
  report_att_id          bigint REFERENCES attachment(att_id),
  report_sha256          char(64) REFERENCES file_blob(sha256),
  report_stage           text REFERENCES dict_stage(code),
  report_is_fallback     boolean,              -- true = 用了受理公示版
  report_file_url        text,
  report_notice_url      text,
  report_entry_code      text,
  report_source_class    text REFERENCES dict_source_class(code),
  report_version_label   text REFERENCES dict_report_version(code),
  seen_pre_approval_notice boolean,           -- 本事项的拟审批公示（或合并公示）是否已经抓到
  report_upgrade_possible  boolean,           -- 用了受理版，该机关发过拟审批公示，而本事项的还没抓到：可能有更好的版本
  approval_att_id        bigint REFERENCES attachment(att_id),
  approval_sha256        char(64) REFERENCES file_blob(sha256),
  approval_file_url      text,
  approval_notice_url    text,
  approval_entry_code    text,
  approval_source_class  text REFERENCES dict_source_class(code),
  pair_confidence        numeric(4,3),         -- 供文件的项目行关联分的最小值
  computed_at            timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT delivered_rows_have_two_traceable_files CHECK (
    delivery_status NOT IN ('COMPLETE','LINK_REVIEW') OR (
          report_sha256       IS NOT NULL AND approval_sha256       IS NOT NULL
      AND report_sha256 IS DISTINCT FROM approval_sha256
      AND report_att_id       IS NOT NULL AND approval_att_id       IS NOT NULL
      AND report_file_url     IS NOT NULL AND approval_file_url     IS NOT NULL
      AND report_notice_url   IS NOT NULL AND approval_notice_url   IS NOT NULL
      AND report_entry_code   IS NOT NULL AND approval_entry_code   IS NOT NULL
      AND report_source_class IS NOT NULL AND approval_source_class IS NOT NULL
      AND report_stage IN ('APPROVAL','PRE_APPROVAL','ACCEPT_PRE_APPROVAL','ACCEPT')
      AND pair_confidence >= CASE WHEN delivery_status = 'COMPLETE' THEN 0.9 ELSE 0.6 END))
);

CREATE TABLE case_gap (                        -- 在范围内却没交付的，必须说清楚为什么
  case_id          bigint NOT NULL REFERENCES eia_case(case_id),
  role             text NOT NULL CHECK (role IN ('report','approval')),
  reason_code      text NOT NULL REFERENCES dict_gap_reason(code),
  detail           text,
  evidence_url     text,                        -- 证明「查过了」的页面
  checked_entries  text[] NOT NULL DEFAULT '{}',
  next_check_at    timestamptz,
  attempts         int NOT NULL DEFAULT 0,
  PRIMARY KEY (case_id, role)
);

CREATE TABLE review_task (
  task_id      bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  task_type    text NOT NULL CHECK (task_type IN
                 ('LINK_LOW_CONF','LINK_MISSING','STAGE_UNKNOWN','ROLE_UNKNOWN','AUTHORITY_UNRESOLVED','FILE_BROKEN')),
  case_id      bigint REFERENCES eia_case(case_id),
  item_id      bigint REFERENCES notice_item(item_id),
  att_id       bigint REFERENCES attachment(att_id),
  payload      jsonb NOT NULL DEFAULT '{}',
  status       text NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','wontfix')),
  assignee     text,
  resolution   text,
  created_at   timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resolved_at  timestamptz
);

-- ---------------------------------------------------------------------
-- 视图
-- ---------------------------------------------------------------------
-- 交付候选：refresh 时整表 upsert 进 case_delivery
CREATE VIEW v_delivery_candidate AS
WITH picked AS (
  SELECT cd.case_id, cd.role, cd.att_id, cd.source_stage, att.item_id, att.notice_id,
         att.blob_sha256, att.url AS file_url, n.url AS notice_url, e.entry_code, s.source_class, att.version_label
  FROM case_document cd
  JOIN attachment att ON att.att_id = cd.att_id
  JOIN notice n       ON n.notice_id = att.notice_id
  JOIN entry e        ON e.entry_id = n.entry_id
  JOIN site s         ON s.site_id = e.site_id
  WHERE cd.selected
),
rep AS (SELECT * FROM picked WHERE role = 'report'),
apv AS (SELECT * FROM picked WHERE role = 'approval'),
conf AS (                                      -- 供文件的公告里，挂到本 case 的项目行关联分取最小
  SELECT p.case_id, MIN(l.score) AS pair_confidence
  FROM picked p
  JOIN notice_item ni     ON ni.notice_id = p.notice_id AND (p.item_id IS NULL OR ni.item_id = p.item_id)
  JOIN case_item_link l   ON l.item_id = ni.item_id AND l.case_id = p.case_id AND l.state = 'accepted'
  GROUP BY p.case_id
),
seen AS (                                      -- 本事项已经抓到过哪些阶段的公告
  SELECT l.case_id,
         MAX(CASE WHEN n.stage IN ('PRE_APPROVAL','ACCEPT_PRE_APPROVAL') THEN 1 ELSE 0 END) AS seen_pre_approval
  FROM case_item_link l
  JOIN notice_item ni ON ni.item_id = l.item_id
  JOIN notice n       ON n.notice_id = ni.notice_id
  WHERE l.state = 'accepted'
  GROUP BY l.case_id
)
SELECT c.case_id,
       CASE
         WHEN c.scope <> 'IN_SCOPE' THEN 'OUT_OF_SCOPE'
         WHEN rep.att_id IS NOT NULL AND apv.att_id IS NOT NULL AND conf.pair_confidence >= cfg.complete_min_score THEN 'COMPLETE'
         WHEN rep.att_id IS NOT NULL AND apv.att_id IS NOT NULL AND conf.pair_confidence >= cfg.deliver_min_score  THEN 'LINK_REVIEW'
         WHEN rep.att_id IS NOT NULL AND apv.att_id IS NOT NULL THEN 'NO_LINK_EVIDENCE'
         WHEN c.lifecycle <> 'DECIDED' THEN 'PENDING_DECISION'
         WHEN rep.att_id IS NULL AND apv.att_id IS NULL THEN 'MISSING_BOTH'
         WHEN rep.att_id IS NULL THEN 'MISSING_REPORT'
         ELSE 'MISSING_APPROVAL'
       END                           AS delivery_status,
       rep.att_id                    AS report_att_id,
       rep.blob_sha256               AS report_sha256,
       rep.source_stage              AS report_stage,
       (rep.source_stage = 'ACCEPT') AS report_is_fallback,
       rep.file_url                  AS report_file_url,
       rep.notice_url                AS report_notice_url,
       rep.entry_code                AS report_entry_code,
       rep.source_class              AS report_source_class,
       rep.version_label             AS report_version_label,
       (COALESCE(seen.seen_pre_approval, 0) = 1) AS seen_pre_approval_notice,
       (rep.source_stage = 'ACCEPT'
        AND COALESCE(seen.seen_pre_approval, 0) = 0
        AND EXISTS (SELECT 1 FROM notice pn
                    WHERE pn.authority_id = c.authority_id
                      AND pn.stage IN ('PRE_APPROVAL','ACCEPT_PRE_APPROVAL'))) AS report_upgrade_possible,
       apv.att_id                    AS approval_att_id,
       apv.blob_sha256               AS approval_sha256,
       apv.file_url                  AS approval_file_url,
       apv.notice_url                AS approval_notice_url,
       apv.entry_code                AS approval_entry_code,
       apv.source_class              AS approval_source_class,
       conf.pair_confidence
FROM eia_case c
CROSS JOIN app_config cfg
LEFT JOIN rep  ON rep.case_id  = c.case_id
LEFT JOIN apv  ON apv.case_id  = c.case_id
LEFT JOIN conf ON conf.case_id = c.case_id
LEFT JOIN seen ON seen.case_id = c.case_id
WHERE c.merged_into IS NULL;

-- 对外交付：只读这个视图。needs_review = 关联分 0.6 – 0.9，带标记交付
CREATE VIEW v_delivery_export AS
SELECT c.case_code, c.region_code, a.name AS authority_name, c.project_name, c.builder_name,
       c.project_code, c.approval_doc_no_norm, c.approval_date,
       (d.delivery_status = 'LINK_REVIEW') AS needs_review,
       d.*
FROM case_delivery d
JOIN eia_case c  ON c.case_id = d.case_id
JOIN authority a ON a.authority_id = c.authority_id
WHERE d.delivery_status IN ('COMPLETE','LINK_REVIEW');

-- @pg-only-begin
-- 格式校验（SQLite 无正则，测试时跳过）
ALTER TABLE notice_item ADD CONSTRAINT ck_item_project_code
  CHECK (project_code IS NULL OR project_code ~ '^\d{4}-\d{6}-\d{2}-\d{2}-\d{6}$');

-- 覆盖率：分母 = 范围内、已作出决定的 case
CREATE VIEW v_coverage_by_authority_month AS
SELECT c.authority_id,
       to_char(c.approval_date, 'YYYY-MM')                                               AS month,
       COUNT(*)                                                                          AS decided_in_scope,
       COUNT(*) FILTER (WHERE d.delivery_status = 'COMPLETE')                            AS complete,
       COUNT(*) FILTER (WHERE d.delivery_status = 'LINK_REVIEW')                         AS delivered_needs_review,
       COUNT(*) FILTER (WHERE d.delivery_status IN ('COMPLETE','LINK_REVIEW') AND d.report_is_fallback) AS delivered_fallback,
       COUNT(*) FILTER (WHERE d.delivery_status IN ('COMPLETE','LINK_REVIEW') AND d.report_upgrade_possible) AS delivered_upgrade_possible,
       COUNT(*) FILTER (WHERE d.delivery_status IN ('MISSING_REPORT','MISSING_BOTH'))    AS missing_report,
       COUNT(*) FILTER (WHERE d.delivery_status IN ('MISSING_APPROVAL','MISSING_BOTH'))  AS missing_approval,
       COUNT(*) FILTER (WHERE d.delivery_status = 'NO_LINK_EVIDENCE')                    AS no_link_evidence
FROM eia_case c
LEFT JOIN case_delivery d ON d.case_id = c.case_id
WHERE c.lifecycle = 'DECIDED' AND c.merged_into IS NULL AND c.scope = 'IN_SCOPE'
GROUP BY c.authority_id, to_char(c.approval_date, 'YYYY-MM');

-- 历史回溯目标：批复类栏目回到 scope_start_date，其余栏目再往前 report_lookback_months
CREATE VIEW v_entry_backfill AS
SELECT e.entry_id, e.entry_code, e.stage_hint, e.status, e.backfill_status, e.window_earliest,
       t.backfill_target,
       (e.window_earliest IS NOT NULL AND e.window_earliest <= t.backfill_target) AS reaches_target
FROM entry e
CROSS JOIN app_config cfg
CROSS JOIN LATERAL (
  SELECT CASE WHEN e.stage_hint = 'APPROVAL' THEN cfg.scope_start_date
              ELSE (cfg.scope_start_date - make_interval(months => cfg.report_lookback_months))::date
         END AS backfill_target
) t
WHERE e.scope_hint IS NULL;

-- 写入交付状态时再核一遍：两份文件已校验、在环保局官方网站、事项在范围内
CREATE FUNCTION trg_delivery_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.delivery_status IN ('COMPLETE','LINK_REVIEW') AND (
       NOT EXISTS (SELECT 1 FROM file_blob WHERE sha256 = NEW.report_sha256   AND verified)
    OR NOT EXISTS (SELECT 1 FROM file_blob WHERE sha256 = NEW.approval_sha256 AND verified)
    OR NOT EXISTS (SELECT 1 FROM attachment a
                     JOIN notice n ON n.notice_id = a.notice_id
                     JOIN entry e  ON e.entry_id = n.entry_id
                     JOIN site s   ON s.site_id = e.site_id
                     JOIN dict_source_class sc ON sc.code = s.source_class
                   WHERE a.att_id = NEW.report_att_id AND a.blob_sha256 = NEW.report_sha256
                     AND a.on_official_host AND sc.deliverable)
    OR NOT EXISTS (SELECT 1 FROM attachment a
                     JOIN notice n ON n.notice_id = a.notice_id
                     JOIN entry e  ON e.entry_id = n.entry_id
                     JOIN site s   ON s.site_id = e.site_id
                     JOIN dict_source_class sc ON sc.code = s.source_class
                   WHERE a.att_id = NEW.approval_att_id AND a.blob_sha256 = NEW.approval_sha256
                     AND a.on_official_host AND sc.deliverable)
    OR EXISTS (SELECT 1 FROM eia_case WHERE case_id = NEW.case_id AND scope <> 'IN_SCOPE')) THEN
    RAISE EXCEPTION 'case % 标记为交付，但文件未校验、不在官方网站，或事项不在范围内', NEW.case_id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER delivery_guard BEFORE INSERT OR UPDATE ON case_delivery
  FOR EACH ROW EXECUTE FUNCTION trg_delivery_guard();
-- @pg-only-end
