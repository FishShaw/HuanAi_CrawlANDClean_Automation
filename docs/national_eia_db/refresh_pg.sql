-- =====================================================================
-- refresh_pg.sql   v0.4   每批「关联」跑完后执行；幂等，可重复跑。
-- PostgreSQL 14+ 与 SQLite 3.35+ 通用（冒烟测试直接执行本文件）。
-- 前置：case_item_link / case_document 已由匹配程序写好；
--       notice_item 的 is_radiation / approval_mode / decision 已按 dict_scope_rule 标好；
--       attachment.on_official_host 已在下载时判定。
-- =====================================================================

-- 0) 从已接受的项目行汇总事项标记，再算范围
UPDATE eia_case SET
  is_radiation = EXISTS (
    SELECT 1 FROM case_item_link l JOIN notice_item ni ON ni.item_id = l.item_id
    WHERE l.case_id = eia_case.case_id AND l.state = 'accepted' AND ni.is_radiation),
  approval_mode = CASE WHEN EXISTS (
    SELECT 1 FROM case_item_link l JOIN notice_item ni ON ni.item_id = l.item_id
    WHERE l.case_id = eia_case.case_id AND l.state = 'accepted' AND ni.approval_mode = 'COMMITMENT')
    THEN 'COMMITMENT' ELSE 'STANDARD' END,
  decision = CASE
    WHEN EXISTS (SELECT 1 FROM case_item_link l JOIN notice_item ni ON ni.item_id = l.item_id
                 WHERE l.case_id = eia_case.case_id AND l.state = 'accepted' AND ni.decision = 'REJECT')   THEN 'REJECT'
    WHEN EXISTS (SELECT 1 FROM case_item_link l JOIN notice_item ni ON ni.item_id = l.item_id
                 WHERE l.case_id = eia_case.case_id AND l.state = 'accepted' AND ni.decision = 'WITHDRAW') THEN 'WITHDRAW'
    WHEN EXISTS (SELECT 1 FROM case_item_link l JOIN notice_item ni ON ni.item_id = l.item_id
                 WHERE l.case_id = eia_case.case_id AND l.state = 'accepted' AND ni.decision = 'APPROVE')  THEN 'APPROVE'
    ELSE decision END
WHERE merged_into IS NULL;

UPDATE eia_case SET scope = CASE
    WHEN is_radiation                           THEN 'OUT_RADIATION'
    WHEN approval_mode = 'COMMITMENT'           THEN 'OUT_COMMITMENT'
    WHEN decision IN ('REJECT','WITHDRAW')      THEN 'OUT_REJECTED'
    WHEN COALESCE(approval_date, pre_approval_date, accept_date)
         < (SELECT scope_start_date FROM app_config WHERE id = 1) THEN 'OUT_BEFORE_START'
    ELSE 'IN_SCOPE' END
WHERE merged_into IS NULL;

-- 1) 清空上一次的选择
UPDATE case_document SET selected = false WHERE selected;

-- 2) 每个范围内事项 × 角色选一份文件
--    只选：已校验 + 文件地址在官方域名 + 网站来源等级可交付
--    报告：dict_stage.report_rank（1 批复公告 → 2 拟审批 / 合并公示 → 3 受理）→ 版本（报批稿 → 送审稿 → 公示稿）
--          → 发布日期新 → 有 PDF → 页数多。后来抓到更好的版本，重跑本脚本即自动替换
--    审批意见：只从 approval_source = true 的阶段取 → 发布日期新 → 有 PDF → 页数多
WITH ranked AS (
  SELECT cd.case_id, cd.att_id, cd.role,
         ROW_NUMBER() OVER (
           PARTITION BY cd.case_id, cd.role
           ORDER BY CASE WHEN cd.role = 'report' THEN ds.report_rank ELSE 0 END,
                    CASE WHEN cd.role = 'report' THEN dv.version_rank ELSE 0 END,
                    n.pub_date DESC NULLS LAST,
                    CASE WHEN b.pdf_sha256 IS NOT NULL OR b.ext = 'pdf' THEN 0 ELSE 1 END,
                    b.pages DESC NULLS LAST,
                    cd.att_id
         ) AS rn
  FROM case_document cd
  JOIN dict_stage ds         ON ds.code = cd.source_stage
  JOIN attachment att        ON att.att_id = cd.att_id
  JOIN dict_report_version dv ON dv.code = att.version_label
  JOIN notice n              ON n.notice_id = att.notice_id
  JOIN entry e               ON e.entry_id = n.entry_id
  JOIN site s                ON s.site_id = e.site_id
  JOIN dict_source_class sc  ON sc.code = s.source_class
  JOIN file_blob b           ON b.sha256 = att.blob_sha256
  JOIN eia_case c            ON c.case_id = cd.case_id AND c.merged_into IS NULL AND c.scope = 'IN_SCOPE'
  WHERE b.verified
    AND att.on_official_host
    AND sc.deliverable
    AND (   (cd.role = 'report'   AND ds.report_rank IS NOT NULL)
         OR (cd.role = 'approval' AND ds.approval_source))
)
UPDATE case_document SET selected = true
FROM ranked r
WHERE r.rn = 1
  AND case_document.case_id = r.case_id
  AND case_document.att_id  = r.att_id
  AND case_document.role    = r.role;

-- 3) 生成交付行（表约束 + 触发器会拒绝不合格的交付）
INSERT INTO case_delivery (
  case_id, delivery_status,
  report_att_id, report_sha256, report_stage, report_is_fallback,
  report_file_url, report_notice_url, report_entry_code, report_source_class,
  report_version_label, seen_pre_approval_notice, report_upgrade_possible,
  approval_att_id, approval_sha256, approval_file_url, approval_notice_url, approval_entry_code, approval_source_class,
  pair_confidence, computed_at)
SELECT case_id, delivery_status,
       report_att_id, report_sha256, report_stage, report_is_fallback,
       report_file_url, report_notice_url, report_entry_code, report_source_class,
       report_version_label, seen_pre_approval_notice, report_upgrade_possible,
       approval_att_id, approval_sha256, approval_file_url, approval_notice_url, approval_entry_code, approval_source_class,
       pair_confidence, CURRENT_TIMESTAMP
FROM v_delivery_candidate
WHERE true
ON CONFLICT (case_id) DO UPDATE SET
  delivery_status       = excluded.delivery_status,
  report_att_id         = excluded.report_att_id,
  report_sha256         = excluded.report_sha256,
  report_stage          = excluded.report_stage,
  report_is_fallback    = excluded.report_is_fallback,
  report_file_url       = excluded.report_file_url,
  report_notice_url     = excluded.report_notice_url,
  report_entry_code     = excluded.report_entry_code,
  report_source_class   = excluded.report_source_class,
  report_version_label  = excluded.report_version_label,
  seen_pre_approval_notice = excluded.seen_pre_approval_notice,
  report_upgrade_possible  = excluded.report_upgrade_possible,
  approval_att_id       = excluded.approval_att_id,
  approval_sha256       = excluded.approval_sha256,
  approval_file_url     = excluded.approval_file_url,
  approval_notice_url   = excluded.approval_notice_url,
  approval_entry_code   = excluded.approval_entry_code,
  approval_source_class = excluded.approval_source_class,
  pair_confidence       = excluded.pair_confidence,
  computed_at           = excluded.computed_at;

-- 4) 缺口：已补上或已出范围的删掉；新缺的登记
DELETE FROM case_gap AS g
WHERE EXISTS (
  SELECT 1 FROM case_delivery d
  WHERE d.case_id = g.case_id
    AND (   d.delivery_status = 'OUT_OF_SCOPE'
         OR (g.role = 'report'   AND d.report_att_id   IS NOT NULL)
         OR (g.role = 'approval' AND d.approval_att_id IS NOT NULL)));

--    自动原因的判定顺序：待决定 → 官方网站上有候选但没选上（校验失败）→ 只有非官方候选 → 还没抓到
--    人工或回溯任务写的原因（OUT_OF_WINDOW / NOT_PUBLISHED 等）不被覆盖
INSERT INTO case_gap (case_id, role, reason_code, next_check_at)
SELECT d.case_id, x.role,
       CASE
         WHEN x.role = 'approval' AND d.delivery_status = 'PENDING_DECISION' THEN 'PENDING_DECISION'
         WHEN EXISTS (
           SELECT 1 FROM case_document cd
           JOIN attachment att       ON att.att_id = cd.att_id
           JOIN notice n             ON n.notice_id = att.notice_id
           JOIN entry e              ON e.entry_id = n.entry_id
           JOIN site s               ON s.site_id = e.site_id
           JOIN dict_source_class sc ON sc.code = s.source_class
           WHERE cd.case_id = d.case_id AND cd.role = x.role AND att.on_official_host AND sc.deliverable)
           THEN 'FILE_NOT_VERIFIED'
         WHEN EXISTS (SELECT 1 FROM case_document cd WHERE cd.case_id = d.case_id AND cd.role = x.role)
           THEN 'NOT_OFFICIAL_SOURCE'
         ELSE 'NOT_YET_CRAWLED'
       END,
       CURRENT_TIMESTAMP
FROM case_delivery d
JOIN (SELECT 'report' AS role UNION ALL SELECT 'approval') x
  ON (x.role = 'report' AND d.report_att_id IS NULL) OR (x.role = 'approval' AND d.approval_att_id IS NULL)
WHERE d.delivery_status NOT IN ('OUT_OF_SCOPE','NO_LINK_EVIDENCE')
ON CONFLICT (case_id, role) DO UPDATE SET reason_code = excluded.reason_code
WHERE case_gap.reason_code IN ('NOT_YET_CRAWLED','PENDING_DECISION','FILE_NOT_VERIFIED','NOT_OFFICIAL_SOURCE');

-- 5) 复核任务（不重复开）：低分关联带标记交付；有文件但没有关联证据
INSERT INTO review_task (task_type, case_id, payload)
SELECT CASE WHEN d.delivery_status = 'LINK_REVIEW' THEN 'LINK_LOW_CONF' ELSE 'LINK_MISSING' END, d.case_id, '{}'
FROM case_delivery d
WHERE d.delivery_status IN ('LINK_REVIEW','NO_LINK_EVIDENCE')
  AND NOT EXISTS (
    SELECT 1 FROM review_task t
    WHERE t.case_id = d.case_id AND t.status = 'open'
      AND t.task_type = CASE WHEN d.delivery_status = 'LINK_REVIEW' THEN 'LINK_LOW_CONF' ELSE 'LINK_MISSING' END);
