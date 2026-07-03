"use client";

import * as React from "react";
import { AnimatePresence, motion } from "framer-motion";
import { ChevronDown, Scale, ShieldAlert } from "lucide-react";
import {
  useContractReviewStore,
  type ContractReviewRunSnapshot,
} from "@/lib/contract-review-store";
import { RiskBadge, RiskBadgeGroup, riskBorderColor } from "@/components/contract/risk-badge";
import type { ConsistencyOpinion, ContractReport, ReviewOpinion } from "@/lib/types";
import { cn } from "@/lib/utils";

export function ContractRiskReportBubble({ run }: { run?: ContractReviewRunSnapshot }) {
  const storeStatus = useContractReviewStore((s) => s.status);
  const storeReportReady = useContractReviewStore((s) => s.reportReady);
  const storeReport = useContractReviewStore((s) => s.report);
  const storeSummary = useContractReviewStore((s) => s.summary);

  const status = run?.status ?? storeStatus;
  const reportReady = run?.reportReady ?? storeReportReady;
  const report = run?.report ?? storeReport;
  const summary = run?.summary ?? storeSummary;

  // 报告必须等全文一致性审查结束后才渲染；report_ready 事件只用于预取条款意见。
  if (!(status === "done" || reportReady) || !report) return null;
  const totalOpinions = report.opinions.length + report.consistency_opinions.length;
  const reportTitle =
    summary?.title || summary?.filename || report.contract.title || report.contract.filename || "合同";

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.24, ease: [0.2, 0.7, 0.2, 1] }}
      className="flex items-start gap-3"
    >
      <div
        className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-white"
        style={{
          background: "linear-gradient(135deg, #7C8CFF 0%, #b482ff 100%)",
          boxShadow: "0 4px 18px -4px rgba(124, 140, 255, 0.5)",
        }}
      >
        <Scale className="h-3.5 w-3.5" />
      </div>

      <div
        className="min-w-0 flex-1 overflow-hidden"
        style={{
          background: "rgba(255, 255, 255, 0.04)",
          border: "1px solid rgba(255, 255, 255, 0.06)",
          borderRadius: "20px",
          borderTopLeftRadius: "6px",
          backdropFilter: "blur(10px)",
          WebkitBackdropFilter: "blur(10px)",
        }}
      >
        <div
          className="flex items-center gap-2 px-4 py-3"
          style={{ borderBottom: "1px solid rgba(255,255,255,0.06)" }}
        >
          <ShieldAlert className="h-3.5 w-3.5 shrink-0 text-amber-400" />
          <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-[var(--color-fg)]">
            《{reportTitle}》审查报告
          </span>
          <span className="shrink-0 text-[12px] text-[var(--color-fg-muted)]">
            共 {totalOpinions} 条意见
          </span>
        </div>

        <RiskFooter report={report} />
      </div>
    </motion.div>
  );
}

/** 把意见按所属条款分组，并按条款在合同中的自然顺序排列；带出每条款的风险评估用于徽章。 */
function groupByClause(report: ContractReport) {
  const { opinions, clauses, clause_risk_assessments: assessments } = report;
  const groups = clauses
    .map((clause) => ({
      clause,
      opinions: opinions.filter(
        (o) =>
          o.clause_id_ref === clause.id ||
          (o.clause_id != null && o.clause_id === clause.clause_id),
      ),
      assessments: assessments.filter(
        (a) =>
          a.clause_id_ref === clause.id ||
          (a.clause_id != null && a.clause_id === clause.clause_id),
      ),
    }))
    .filter((g) => g.opinions.length > 0);

  // 兜底：未能匹配到任何条款的意见（理论上不该出现，但避免静默丢失）。
  const matched = new Set(groups.flatMap((g) => g.opinions.map((o) => o)));
  const orphans = opinions.filter((o) => !matched.has(o));
  return { groups, orphans };
}

function RiskFooter({ report }: { report: ContractReport }) {
  // 审查报告默认折叠：点开题头的风险徽章行才展开条款分组。
  const [open, setOpen] = React.useState(false);
  const opinions = report.opinions;
  const consistencyOpinions = report.consistency_opinions;
  const consistencyRisk = report.consistency_risk_assessment ?? null;
  const hasConsistencyDetail =
    consistencyOpinions.length > 0 || Boolean(consistencyRisk?.rationale?.trim());
  const hasFindings = opinions.length > 0 || hasConsistencyDetail;

  const counts = report.clause_risk_assessments.reduce<Record<string, number>>((acc, r) => {
    acc[r.risk_level] = (acc[r.risk_level] ?? 0) + 1;
    return acc;
  }, {});
  if (consistencyRisk) {
    counts[consistencyRisk.risk_level] = (counts[consistencyRisk.risk_level] ?? 0) + 1;
  }

  const { groups, orphans } = groupByClause(report);

  return (
    <div>
      <button
        type="button"
        className="flex w-full items-center gap-2 px-4 py-3"
        onClick={() => hasFindings && setOpen((value) => !value)}
      >
        {!hasFindings ? (
          <span className="text-[12.5px] text-emerald-400">
            未发现条款级或全文一致性风险
          </span>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {(["critical", "high", "medium", "low", "none"] as const).map((level) =>
              counts[level] ? <RiskBadge key={level} level={level} count={counts[level]} /> : null,
            )}
          </div>
        )}

        {hasFindings && (
          <ChevronDown
            className={cn(
              "ml-auto h-3.5 w-3.5 shrink-0 text-[var(--color-fg-faint)] transition-transform duration-200",
              open && "rotate-180",
            )}
          />
        )}
      </button>

      <AnimatePresence initial={false}>
        {open && hasFindings && (
          <motion.div
            key="risks"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.22, ease: [0.2, 0.7, 0.2, 1] }}
            className="overflow-hidden"
          >
            <div className="space-y-3 px-3 pb-3">
              {groups.map((g) => (
                <ClauseGroup
                  key={g.clause.id}
                  clauseNo={g.clause.clause_no}
                  title={g.clause.title}
                  opinions={g.opinions}
                  assessments={g.assessments}
                />
              ))}
              {orphans.length > 0 && (
                <ClauseGroup
                  clauseNo=""
                  title="其他意见"
                  opinions={orphans}
                  assessments={[]}
                />
              )}
              {hasConsistencyDetail && (
                <ConsistencyGroup
                  opinions={consistencyOpinions}
                  riskAssessment={consistencyRisk}
                />
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

/** 单条款分组：条款标题（编号 + 名称 + 风险徽章）作为可折叠小节头，下挂该条款的意见卡片。 */
function ClauseGroup({
  clauseNo,
  title,
  opinions,
  assessments,
}: {
  clauseNo: string;
  title: string;
  opinions: ReviewOpinion[];
  assessments: ContractReport["clause_risk_assessments"];
}) {
  // 每个条款分组默认折叠：展开报告后再逐条点开查看意见。
  const [open, setOpen] = React.useState(false);
  const topLevel = assessments[0]?.risk_level;
  return (
    <div
      className={cn(
        "rounded-xl border border-white/[0.06] bg-white/[0.015]",
        riskBorderColor(topLevel),
      )}
      style={{ borderLeftWidth: "3px" }}
    >
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left"
      >
        {clauseNo && (
          <span className="shrink-0 text-[12px] font-semibold text-[var(--color-fg-muted)]">
            {clauseNo}
          </span>
        )}
        <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium text-[var(--color-fg)]">
          {title || "未命名条款"}
        </span>
        <RiskBadgeGroup assessments={assessments} className="shrink-0" />
        <span className="shrink-0 text-[11px] text-[var(--color-fg-faint)]">
          {opinions.length} 条
        </span>
        <ChevronDown
          className={cn(
            "h-3.5 w-3.5 shrink-0 text-[var(--color-fg-faint)] transition-transform duration-200",
            open && "rotate-180",
          )}
        />
      </button>

      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            key="opinions"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2, ease: [0.2, 0.7, 0.2, 1] }}
            className="overflow-hidden"
          >
            <div className="space-y-2 px-2 pb-2">
              {opinions.map((opinion, i) => (
                <RiskCard key={opinion.id ?? i} opinion={opinion} />
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

/** 报告中的全文一致性审查结果，补齐跨条款冲突/缺失/需核实事项。 */
function ConsistencyGroup({
  opinions,
  riskAssessment,
}: {
  opinions: ConsistencyOpinion[];
  riskAssessment: ContractReport["consistency_risk_assessment"];
}) {
  const [open, setOpen] = React.useState(false);
  const level = riskAssessment?.risk_level;
  const hasOpinions = opinions.length > 0;
  return (
    <div
      className={cn(
        "rounded-xl border border-white/[0.06] bg-white/[0.015]",
        riskBorderColor(level),
      )}
      style={{ borderLeftWidth: "3px" }}
    >
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left"
      >
        <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium text-[var(--color-fg)]">
          全文一致性审查
        </span>
        {level && <RiskBadge level={level} className="shrink-0" />}
        <span className="shrink-0 text-[11px] text-[var(--color-fg-faint)]">
          {opinions.length} 条
        </span>
        <ChevronDown
          className={cn(
            "h-3.5 w-3.5 shrink-0 text-[var(--color-fg-faint)] transition-transform duration-200",
            open && "rotate-180",
          )}
        />
      </button>

      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            key="consistency-opinions"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2, ease: [0.2, 0.7, 0.2, 1] }}
            className="overflow-hidden"
          >
            <div className="space-y-2 px-2 pb-2">
              {riskAssessment?.rationale && (
                <p className="rounded-lg border border-white/[0.05] bg-white/[0.02] px-3 py-2 text-[12px] leading-relaxed text-[var(--color-fg-muted)]">
                  {riskAssessment.rationale}
                </p>
              )}
              {hasOpinions ? (
                opinions.map((opinion, index) => (
                  <ConsistencyRiskCard key={opinion.id ?? index} opinion={opinion} />
                ))
              ) : (
                <p className="px-1 text-[12px] text-emerald-400">未发现跨条款一致性问题。</p>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function RiskCard({ opinion }: { opinion: ReviewOpinion }) {
  return (
    <div className="rounded-xl border border-white/[0.06] bg-white/[0.02] px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[12px] font-medium text-[var(--color-fg)]">{opinion.opinion_type}</span>
        <span className="rounded bg-white/[0.06] px-1.5 py-0.5 text-[11px] text-[var(--color-fg-faint)]">
          {opinion.review_dimension}
        </span>
      </div>
      <p className="mt-1.5 text-[12.5px] leading-relaxed text-[var(--color-fg-muted)]">
        {opinion.finding}
      </p>
      {opinion.recommendation && (
        <p className="mt-1 text-[12px] leading-relaxed text-[var(--color-fg-faint)]">
          建议：{opinion.recommendation}
        </p>
      )}
    </div>
  );
}

function ConsistencyRiskCard({ opinion }: { opinion: ConsistencyOpinion }) {
  return (
    <div className="rounded-xl border border-white/[0.06] bg-white/[0.02] px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[12px] font-medium text-[var(--color-fg)]">
          {opinion.opinion_type}
        </span>
        <span className="rounded bg-white/[0.06] px-1.5 py-0.5 text-[11px] text-[var(--color-fg-faint)]">
          {opinion.review_dimension}
        </span>
        {opinion.related_clause_ids.length > 0 && (
          <span className="text-[11px] text-[var(--color-fg-faint)]">
            关联：{opinion.related_clause_ids.join("、")}
          </span>
        )}
      </div>
      <p className="mt-1.5 text-[12.5px] leading-relaxed text-[var(--color-fg-muted)]">
        {opinion.finding}
      </p>
      {opinion.evidence_facts.length > 0 && (
        <p className="mt-1 text-[11.5px] leading-relaxed text-[var(--color-fg-faint)]">
          依据：{opinion.evidence_facts.join("；")}
        </p>
      )}
      {opinion.recommendation && (
        <p className="mt-1 text-[12px] leading-relaxed text-[var(--color-fg-faint)]">
          建议：{opinion.recommendation}
        </p>
      )}
    </div>
  );
}
