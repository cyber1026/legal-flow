"use client";

import * as React from "react";
import { motion } from "framer-motion";
import { Scale } from "lucide-react";
import { LawUploader } from "@/components/knowledge/law-uploader";
import { LawsTable } from "@/components/knowledge/laws-table";

export default function KnowledgePage() {
  const [lawRefreshKey, setLawRefreshKey] = React.useState(0);
  return (
    <div className="h-full overflow-y-auto">
      <motion.div
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.3 }}
        className="mx-auto w-full max-w-[900px] space-y-9 px-6 py-9"
      >
        <section className="space-y-5">
          <header className="flex items-start gap-4">
            <div
              className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl text-white"
              style={{
                background: "linear-gradient(135deg, #34d399 0%, #059669 100%)",
                boxShadow: "0 8px 24px -8px rgba(52, 211, 153, 0.5)",
              }}
            >
              <Scale className="h-5 w-5" />
            </div>
            <div className="space-y-1">
              <h1
                className="text-[26px] font-semibold tracking-tight text-[var(--color-fg)]"
                style={{ letterSpacing: "-0.02em" }}
              >
                法律知识库
              </h1>
              <p className="text-[13.5px] text-[var(--color-fg-muted)]">
                上传法律文件，系统自动按条文结构解析并向量化入库，用于合同审查时的法条检索与引用。
              </p>
            </div>
          </header>
          <LawUploader onComplete={() => setLawRefreshKey((x) => x + 1)} />
          <LawsTable refreshKey={lawRefreshKey} />
        </section>
      </motion.div>
    </div>
  );
}
