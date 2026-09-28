// Category Icons
function CategoryIcon({ category, stage }) {
  if (stage === "user_request" || category === "user") return "👤";
  if (stage === "init" || (category === "orchestration" && stage !== "evidence")) return "⚙️";
  if (stage === "mcp_start" || category === "mcp") return "🔌";
  if (stage === "llm_request" || category === "llm") return "🧠";
  if (stage === "tool_execution" || category === "tool") return "⚡";
  if (stage === "evidence") return "🔍";
  if (stage === "mcp_cleanup") return "🧹";
  if (stage === "terminal") return "🏁";
  return "•";
}

// State Badges
function StateBadge({ status }) {
  if (status === "active") {
    return (
      <span
        style={{ backgroundColor: "rgba(59, 130, 246, 0.2)", borderColor: "#60a5fa", color: "#93c5fd" }}
        className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium border shadow-sm animate-pulse"
      >
        <span className="h-1.5 w-1.5 rounded-full bg-blue-400" />
        Active
      </span>
    );
  }
  if (status === "completed") {
    return (
      <span
        style={{ backgroundColor: "rgba(16, 185, 129, 0.18)", borderColor: "#10b981", color: "#6ee7b7" }}
        className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium border"
      >
        <span className="font-bold text-emerald-400">✓</span> Done
      </span>
    );
  }
  if (status === "failed") {
    return (
      <span
        style={{ backgroundColor: "rgba(239, 68, 68, 0.18)", borderColor: "#ef4444", color: "#fca5a5" }}
        className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium border"
      >
        <span className="font-bold text-red-400">✕</span> Failed
      </span>
    );
  }
  if (status === "stopped") {
    return (
      <span
        style={{ backgroundColor: "rgba(245, 158, 11, 0.18)", borderColor: "#f59e0b", color: "#fde68a" }}
        className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium border"
      >
        <span className="text-amber-400">■</span> Bounded
      </span>
    );
  }
  if (status === "cancelled") {
    return (
      <span
        style={{ backgroundColor: "rgba(113, 113, 122, 0.18)", borderColor: "#71717a", color: "#d4d4d8" }}
        className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium border"
      >
        <span>■</span> Cancelled
      </span>
    );
  }
  return (
    <span
      style={{ backgroundColor: "rgba(82, 82, 91, 0.15)", borderColor: "#52525b", color: "#a1a1aa" }}
      className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium border"
    >
      <span>○</span> Waiting
    </span>
  );
}

// Category-specific color theme for nodes
function getNodeStyles(node, isSelected) {
  if (node.status === "active") {
    return {
      style: {
        backgroundColor: "rgba(14, 165, 233, 0.25)",
        borderColor: "#38bdf8",
        boxShadow: isSelected ? "0 0 16px rgba(56, 189, 248, 0.6)" : "0 0 10px rgba(56, 189, 248, 0.3)",
      },
      titleColor: "#e0f2fe",
      subtitleColor: "#7dd3fc",
      iconColor: "text-sky-300",
      durationStyle: { backgroundColor: "rgba(12, 74, 110, 0.6)", color: "#bae6fd" },
      activeRing: "ring-2 ring-sky-400 animate-pulse",
    };
  }

  if (node.status === "failed") {
    return {
      style: {
        backgroundColor: "rgba(127, 29, 29, 0.38)",
        borderColor: "#ef4444",
        boxShadow: isSelected ? "0 0 16px rgba(239, 68, 68, 0.6)" : "0 0 8px rgba(239, 68, 68, 0.2)",
      },
      titleColor: "#fecaca",
      subtitleColor: "#fca5a5",
      iconColor: "text-red-400",
      durationStyle: { backgroundColor: "rgba(127, 29, 29, 0.6)", color: "#fee2e2" },
      activeRing: isSelected ? "ring-2 ring-red-400" : "",
    };
  }

  // 1. User Request: Blue
  if (node.stage === "user_request" || node.category === "user") {
    return {
      style: {
        backgroundColor: "rgba(30, 58, 138, 0.35)",
        borderColor: "#3b82f6",
        boxShadow: isSelected ? "0 0 16px rgba(59, 130, 246, 0.5)" : "none",
      },
      titleColor: "#dbeafe",
      subtitleColor: "#93c5fd",
      iconColor: "text-blue-400",
      durationStyle: { backgroundColor: "rgba(30, 58, 138, 0.6)", color: "#bfdbfe" },
      activeRing: isSelected ? "ring-2 ring-blue-400" : "",
    };
  }

  // 2. Initialize Investigation: Blue
  if (node.stage === "init" || (node.category === "orchestration" && node.stage !== "evidence")) {
    return {
      style: {
        backgroundColor: "rgba(30, 58, 138, 0.35)",
        borderColor: "#2563eb",
        boxShadow: isSelected ? "0 0 16px rgba(37, 99, 235, 0.5)" : "none",
      },
      titleColor: "#dbeafe",
      subtitleColor: "#93c5fd",
      iconColor: "text-blue-400",
      durationStyle: { backgroundColor: "rgba(30, 58, 138, 0.6)", color: "#bfdbfe" },
      activeRing: isSelected ? "ring-2 ring-blue-400" : "",
    };
  }

  // 3. MCP Connections: Teal
  if (node.stage === "mcp_start" || node.category === "mcp") {
    return {
      style: {
        backgroundColor: "rgba(19, 78, 74, 0.35)",
        borderColor: "#14b8a6",
        boxShadow: isSelected ? "0 0 16px rgba(20, 184, 166, 0.5)" : "none",
      },
      titleColor: "#ccfbf1",
      subtitleColor: "#5eead4",
      iconColor: "text-teal-400",
      durationStyle: { backgroundColor: "rgba(19, 78, 74, 0.6)", color: "#a7f3d0" },
      activeRing: isSelected ? "ring-2 ring-teal-400" : "",
    };
  }

  // 4. LLM Calls: Emerald Green
  if (node.stage === "llm_request" || node.category === "llm") {
    return {
      style: {
        backgroundColor: "rgba(6, 78, 59, 0.35)",
        borderColor: "#10b981",
        boxShadow: isSelected ? "0 0 16px rgba(16, 185, 129, 0.5)" : "none",
      },
      titleColor: "#d1fae5",
      subtitleColor: "#6ee7b7",
      iconColor: "text-emerald-400",
      durationStyle: { backgroundColor: "rgba(6, 78, 59, 0.6)", color: "#a7f3d0" },
      activeRing: isSelected ? "ring-2 ring-emerald-400" : "",
    };
  }

  // 5. MCP Tool Execution: Sky Blue
  if (node.stage === "tool_execution" || node.category === "tool") {
    return {
      style: {
        backgroundColor: "rgba(12, 74, 110, 0.35)",
        borderColor: "#0ea5e9",
        boxShadow: isSelected ? "0 0 16px rgba(14, 165, 233, 0.5)" : "none",
      },
      titleColor: "#e0f2fe",
      subtitleColor: "#7dd3fc",
      iconColor: "text-sky-400",
      durationStyle: { backgroundColor: "rgba(12, 74, 110, 0.6)", color: "#bae6fd" },
      activeRing: isSelected ? "ring-2 ring-sky-400" : "",
    };
  }

  // 6. Evidence Processing: Purple
  if (node.stage === "evidence") {
    return {
      style: {
        backgroundColor: "rgba(88, 28, 135, 0.35)",
        borderColor: "#a855f7",
        boxShadow: isSelected ? "0 0 16px rgba(168, 85, 247, 0.5)" : "none",
      },
      titleColor: "#f3e8ff",
      subtitleColor: "#d8b4fe",
      iconColor: "text-purple-400",
      durationStyle: { backgroundColor: "rgba(88, 28, 135, 0.6)", color: "#e9d5ff" },
      activeRing: isSelected ? "ring-2 ring-purple-400" : "",
    };
  }

  // 7. MCP Cleanup: Orange
  if (node.stage === "mcp_cleanup") {
    return {
      style: {
        backgroundColor: "rgba(124, 45, 18, 0.35)",
        borderColor: "#f97316",
        boxShadow: isSelected ? "0 0 16px rgba(249, 115, 22, 0.5)" : "none",
      },
      titleColor: "#ffedd5",
      subtitleColor: "#fdba74",
      iconColor: "text-orange-400",
      durationStyle: { backgroundColor: "rgba(124, 45, 18, 0.6)", color: "#fed7aa" },
      activeRing: isSelected ? "ring-2 ring-orange-400" : "",
    };
  }

  // 8. Terminal: Green
  if (node.stage === "terminal") {
    return {
      style: {
        backgroundColor: "rgba(20, 83, 45, 0.4)",
        borderColor: "#22c55e",
        boxShadow: isSelected ? "0 0 16px rgba(34, 197, 94, 0.5)" : "none",
      },
      titleColor: "#dcfce7",
      subtitleColor: "#86efac",
      iconColor: "text-green-400",
      durationStyle: { backgroundColor: "rgba(20, 83, 45, 0.6)", color: "#bbf7d0" },
      activeRing: isSelected ? "ring-2 ring-green-400" : "",
    };
  }

  // Fallback
  return {
    style: {
      backgroundColor: "rgba(30, 41, 59, 0.4)",
      borderColor: "#475569",
      boxShadow: isSelected ? "0 0 16px rgba(100, 116, 139, 0.5)" : "none",
    },
    titleColor: "#f1f5f9",
    subtitleColor: "#94a3b8",
    iconColor: "text-slate-400",
    durationStyle: { backgroundColor: "rgba(30, 41, 59, 0.6)", color: "#cbd5e1" },
    activeRing: isSelected ? "ring-2 ring-slate-400" : "",
  };
}

// Flow connector arrow
function FlowArrow() {
  return (
    <div className="flex flex-col items-center justify-center my-1">
      <div className="w-0.5 h-3 bg-slate-600" />
      <svg className="w-3 h-3 text-slate-400 -mt-0.5" fill="currentColor" viewBox="0 0 12 12">
        <path d="M6 9L2 4h8L6 9z" />
      </svg>
    </div>
  );
}

// Dedicated Full-Screen Execution Viewer Modal Component
function ExecutionViewerModal({ trace, onClose }) {
  const [selectedStepId, setSelectedStepId] = React.useState(null);
  const [searchQuery, setSearchQuery] = React.useState("");
  const [statusFilter, setStatusFilter] = React.useState("all");
  const [expandedCardIds, setExpandedCardIds] = React.useState({});
  const [outputTab, setOutputTab] = React.useState("details");
  const [scale, setScale] = React.useState(1);
  const [copied, setCopied] = React.useState(false);
  const [copiedSummary, setCopiedSummary] = React.useState(false);
  const flowContainerRef = React.useRef(null);

  const nodes = trace?.nodes || [];
  const timeline = trace?.timeline || [];
  const summary = trace?.summary || {};
  const details = trace?.details || {};
  const connections = trace?.connections || {};
  const metadata = trace?.metadata || {};

  // Count errors / warnings
  const errorCount = React.useMemo(() => {
    return nodes.filter(n => n.status === "failed" || n.error).length;
  }, [nodes]);

  // Default select first node or active/failed node
  React.useEffect(() => {
    if (nodes.length > 0 && !selectedStepId) {
      const activeOrFailed = nodes.find(n => n.status === "failed" || n.status === "active");
      setSelectedStepId(activeOrFailed ? activeOrFailed.id : nodes[0].id);
    }
  }, [nodes]);

  // Handle ESC key to close
  React.useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  // Lock body scroll while modal is open
  React.useEffect(() => {
    const origOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = origOverflow;
    };
  }, []);

  // Filtered steps
  const filteredNodes = React.useMemo(() => {
    return nodes.filter((n) => {
      // 1. Status / Category Filter
      if (statusFilter !== "all") {
        if (statusFilter === "completed" && n.status !== "completed") return false;
        if (statusFilter === "active" && n.status !== "active") return false;
        if (statusFilter === "failed" && n.status !== "failed" && !n.error) return false;
        if (statusFilter === "tool" && n.stage !== "tool_execution" && n.category !== "tool") return false;
        if (statusFilter === "llm" && n.stage !== "llm_request" && n.category !== "llm") return false;
        if (statusFilter === "mcp" && n.stage !== "mcp_start" && n.category !== "mcp" && n.stage !== "mcp_cleanup") return false;
      }

      // 2. Search Query
      if (!searchQuery.trim()) return true;
      const q = searchQuery.toLowerCase();
      return (
        (n.title && n.title.toLowerCase().includes(q)) ||
        (n.subtitle && n.subtitle.toLowerCase().includes(q)) ||
        (n.stage && n.stage.toLowerCase().includes(q)) ||
        (n.category && n.category.toLowerCase().includes(q)) ||
        (n.error && n.error.toLowerCase().includes(q))
      );
    });
  }, [nodes, searchQuery, statusFilter]);

  const selectedNode = React.useMemo(() => {
    return nodes.find((n) => n.id === selectedStepId) || nodes[0] || null;
  }, [nodes, selectedStepId]);

  // Group parallel nodes for flow canvas
  const renderedFlowGroups = React.useMemo(() => {
    const groups = [];
    let i = 0;
    while (i < nodes.length) {
      const node = nodes[i];
      if (node.parallel && node.group_id) {
        const groupNodes = [];
        const gid = node.group_id;
        while (i < nodes.length && nodes[i].group_id === gid) {
          groupNodes.push(nodes[i]);
          i++;
        }
        groups.push({ type: "parallel", id: gid, nodes: groupNodes });
      } else {
        groups.push({ type: "single", id: node.id, node: node });
        i++;
      }
    }
    return groups;
  }, [nodes]);

  // Zoom controls
  const handleZoomIn = () => setScale((s) => Math.min(2.0, +(s + 0.15).toFixed(2)));
  const handleZoomOut = () => setScale((s) => Math.max(0.6, +(s - 0.15).toFixed(2)));
  const handleZoomReset = () => setScale(1.0);

  const toggleCardExpand = (id, e) => {
    if (e) e.stopPropagation();
    setExpandedCardIds((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const handleCopyJson = () => {
    const jsonStr = JSON.stringify(selectedNode ? { selected_step: selectedNode, trace } : trace, null, 2);
    if (navigator.clipboard) {
      navigator.clipboard.writeText(jsonStr).then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      });
    }
  };

  const handleCopySummary = () => {
    const summaryText = [
      `DevOps AI Agent — Execution Summary`,
      `Category: ${summary.category || "Investigation"}`,
      `Outcome: ${summary.outcome || "completed"}`,
      `Elapsed Time: ${Number(summary.elapsed_seconds || 0).toFixed(1)}s`,
      `Recorded Steps: ${nodes.length}`,
      `Model Calls: ${summary.model_call_count || 0}`,
      `Tool Calls: ${summary.tool_call_count || 0}`,
      `Access Mode: Read-only`,
    ].join("\n");

    if (navigator.clipboard) {
      navigator.clipboard.writeText(summaryText).then(() => {
        setCopiedSummary(true);
        setTimeout(() => setCopiedSummary(false), 2000);
      });
    }
  };

  const handleExportLogs = () => {
    const exportData = {
      investigation: {
        category: summary.category || "Investigation",
        outcome: summary.outcome || "completed",
        elapsed_seconds: summary.elapsed_seconds || 0,
        total_steps: nodes.length,
        access_mode: "Read-only",
      },
      nodes,
      timeline,
      connections,
      details,
      metadata,
    };
    const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `execution_log_${(summary.category || "investigation").toLowerCase()}_${Date.now()}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  return (
    <div
      style={{ zIndex: 99999 }}
      className="fixed inset-0 flex flex-col bg-[#070b14]/95 backdrop-blur-xl text-slate-200 select-none overflow-hidden"
      role="dialog"
      aria-modal="true"
      aria-labelledby="execution-viewer-title"
    >
      {/* 1. Header & Global Summary Bar */}
      <header className="flex flex-wrap items-center justify-between gap-3 px-6 py-3.5 bg-slate-900/90 border-b border-slate-800 shrink-0 shadow-lg">
        <div className="flex items-center gap-3.5 min-w-0">
          <div className="p-2.5 rounded-xl bg-blue-500/15 border border-blue-500/40 text-blue-400 shadow-inner">
            <span className="text-2xl">📊</span>
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2.5">
              <h1 id="execution-viewer-title" className="text-lg font-bold text-white tracking-tight truncate">
                Execution Viewer
              </h1>
              <span className="px-2.5 py-0.5 rounded-full text-xs font-bold bg-blue-950/80 border border-blue-500/50 text-blue-300 shadow-sm">
                {summary.category || "Investigation"}
              </span>
              <StateBadge status={summary.outcome || "completed"} />
            </div>

            {/* Metrics row */}
            <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400 mt-1">
              <span className="font-medium text-slate-300">{nodes.length} Steps</span>
              <span>·</span>
              <span>{summary.model_call_count || 0} LLM Calls</span>
              <span>·</span>
              <span>{summary.tool_call_count || 0} Tool Calls</span>
              <span>·</span>
              <span className="font-mono text-slate-300">{Number(summary.elapsed_seconds || 0).toFixed(1)}s elapsed</span>
              {errorCount > 0 && (
                <>
                  <span>·</span>
                  <span className="text-red-400 font-semibold bg-red-950/60 px-2 py-0.5 rounded border border-red-500/40">
                    {errorCount} {errorCount === 1 ? "Error" : "Errors"}
                  </span>
                </>
              )}
              <span>·</span>
              <span className="text-emerald-400 font-semibold bg-emerald-950/60 px-2 py-0.5 rounded border border-emerald-500/40">
                Read-only Access
              </span>
            </div>
          </div>
        </div>

        {/* Action Controls */}
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleCopySummary}
            className="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 hover:text-white transition-all text-xs font-medium shadow-sm flex items-center gap-1.5 cursor-pointer"
            title="Copy Execution Summary to Clipboard"
          >
            <span>📋</span>
            <span>{copiedSummary ? "Copied!" : "Copy Summary"}</span>
          </button>

          <button
            type="button"
            onClick={handleExportLogs}
            className="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 hover:text-white transition-all text-xs font-medium shadow-sm flex items-center gap-1.5 cursor-pointer"
            title="Export full execution logs JSON"
          >
            <span>📥</span>
            <span>Export Logs</span>
          </button>

          <button
            type="button"
            onClick={onClose}
            className="p-2 rounded-xl bg-slate-800 hover:bg-red-900/40 border border-slate-700 hover:border-red-500/50 text-slate-300 hover:text-red-200 transition-all cursor-pointer shadow-md flex items-center gap-1.5 text-xs font-semibold"
            title="Close Execution Viewer (Esc)"
            aria-label="Close Execution Viewer"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
            <span className="hidden sm:inline">Close</span>
          </button>
        </div>
      </header>

      {/* 2. Main Split Area */}
      <div className="flex flex-col lg:flex-row flex-1 min-h-0 overflow-hidden">
        {/* Left Panel: Searchable, Filterable Steps List */}
        <aside className="w-full lg:w-96 lg:min-w-[340px] flex flex-col border-b lg:border-b-0 lg:border-r border-slate-800 bg-[#090d18] shrink-0">
          {/* Search & Filter Header */}
          <div className="p-3.5 border-b border-slate-800 space-y-2.5 shrink-0 bg-slate-900/40">
            <div className="relative">
              <span className="absolute inset-y-0 left-0 flex items-center pl-2.5 text-slate-400 pointer-events-none">
                🔍
              </span>
              <input
                type="text"
                placeholder="Search execution steps…"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="w-full pl-8 pr-3 py-1.5 text-xs bg-slate-900 border border-slate-700 rounded-lg text-slate-200 placeholder-slate-500 focus:outline-none focus:ring-1 focus:ring-blue-500 focus:border-blue-500 transition-all"
              />
              {searchQuery && (
                <button
                  type="button"
                  onClick={() => setSearchQuery("")}
                  className="absolute inset-y-0 right-0 flex items-center pr-2.5 text-slate-400 hover:text-slate-200 cursor-pointer"
                >
                  ✕
                </button>
              )}
            </div>

            {/* Filter pills */}
            <div className="flex items-center gap-1 overflow-x-auto pb-0.5 text-[10px] font-medium devops-trace-scroll">
              {[
                { id: "all", label: "All" },
                { id: "completed", label: "Completed" },
                { id: "active", label: "Active" },
                { id: "failed", label: "Failed" },
                { id: "tool", label: "Tools" },
                { id: "llm", label: "LLM" },
                { id: "mcp", label: "MCP" },
              ].map((f) => (
                <button
                  key={f.id}
                  type="button"
                  onClick={() => setStatusFilter(f.id)}
                  className={`px-2 py-0.5 rounded-md transition-all shrink-0 cursor-pointer ${
                    statusFilter === f.id
                      ? "bg-blue-600/30 text-blue-300 border border-blue-500/60 font-semibold shadow-sm"
                      : "bg-slate-800/80 text-slate-400 hover:text-slate-200 border border-slate-700/60"
                  }`}
                >
                  {f.label}
                </button>
              ))}
            </div>

            <div className="flex justify-between items-center text-[10px] text-slate-400 px-1 pt-0.5">
              <span className="font-semibold uppercase tracking-wider text-slate-500">Execution Steps</span>
              <span>
                Showing {filteredNodes.length} of {nodes.length}
              </span>
            </div>
          </div>

          {/* Steps List */}
          <div className="flex-1 overflow-y-auto devops-trace-scroll p-2.5 space-y-2">
            {filteredNodes.length === 0 ? (
              <div className="text-center py-12 text-xs text-slate-500">
                {nodes.length === 0 ? "No execution steps recorded." : "No steps match your search / filter criteria."}
              </div>
            ) : (
              filteredNodes.map((node) => {
                const isSelected = selectedStepId === node.id;
                const isExpanded = Boolean(expandedCardIds[node.id]);
                const styles = getNodeStyles(node, isSelected);
                const stepNumber = nodes.findIndex((n) => n.id === node.id) + 1;

                return (
                  <div
                    key={node.id}
                    onClick={() => setSelectedStepId(node.id)}
                    style={styles.style}
                    className={`rounded-xl border transition-all cursor-pointer flex flex-col p-2.5 gap-1.5 shadow-sm ${
                      isSelected ? "ring-2 ring-blue-400 shadow-lg" : "hover:border-slate-500"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex items-center gap-1.5 min-w-0">
                        <span className="font-mono text-[10px] font-bold px-1.5 py-0.5 rounded bg-black/50 text-slate-300 border border-white/5">
                          #{stepNumber}
                        </span>
                        <span className={`text-sm shrink-0 ${styles.iconColor}`}>
                          <CategoryIcon category={node.category} stage={node.stage} />
                        </span>
                        <span
                          style={{ color: styles.titleColor }}
                          className="font-semibold text-xs truncate leading-tight"
                        >
                          {node.title}
                        </span>
                      </div>
                      <div className="flex items-center gap-1 shrink-0">
                        <StateBadge status={node.status} />
                        <button
                          type="button"
                          onClick={(e) => toggleCardExpand(node.id, e)}
                          className="p-1 rounded text-slate-400 hover:text-white hover:bg-slate-700/50 transition-colors"
                          title={isExpanded ? "Collapse step details" : "Expand step details"}
                          aria-label={isExpanded ? "Collapse step details" : "Expand step details"}
                        >
                          <svg
                            className={`w-3.5 h-3.5 transform transition-transform ${isExpanded ? "rotate-180" : ""}`}
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="2.5"
                            viewBox="0 0 24 24"
                          >
                            <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
                          </svg>
                        </button>
                      </div>
                    </div>

                    {node.subtitle && (
                      <div
                        style={{ color: styles.subtitleColor }}
                        className="text-[11px] truncate leading-tight pl-6"
                      >
                        {node.subtitle}
                      </div>
                    )}

                    <div className="flex items-center justify-between text-[10px] text-slate-400 pl-6">
                      <span>+{Number(node.started_seconds || 0).toFixed(1)}s start</span>
                      {node.duration_seconds !== null && node.duration_seconds !== undefined && (
                        <span
                          style={styles.durationStyle}
                          className="font-mono px-1.5 py-0.5 rounded border border-white/10"
                        >
                          {Number(node.duration_seconds).toFixed(2)}s duration
                        </span>
                      )}
                    </div>

                    {/* Inline Expandable Details */}
                    {isExpanded && (
                      <div
                        onClick={(e) => e.stopPropagation()}
                        className="mt-1.5 pt-1.5 border-t border-slate-700/60 text-[10px] space-y-1 bg-black/20 p-2 rounded-lg"
                      >
                        <div className="flex justify-between">
                          <span className="text-slate-400">Stage:</span>
                          <span className="font-mono text-slate-200">{node.stage}</span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-slate-400">Category:</span>
                          <span className="font-mono text-slate-200">{node.category}</span>
                        </div>
                        {node.error && (
                          <div className="p-1.5 bg-red-950/60 border border-red-500/40 rounded text-red-200 mt-1">
                            <strong>Error:</strong> {node.error}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </aside>

        {/* Right Panel: Flow Graph (Top) + Output Inspector (Bottom) */}
        <main className="flex-1 flex flex-col min-w-0 overflow-hidden bg-[#070b14]">
          {/* Top Section: Visual Flow Diagram Canvas */}
          <section className="flex-1 flex flex-col min-h-[320px] border-b border-slate-800 relative overflow-hidden">
            {/* Toolbar */}
            <div className="flex items-center justify-between px-5 py-2.5 bg-slate-900/70 border-b border-slate-800 shrink-0 z-10">
              <div className="flex items-center gap-2">
                <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">
                  Execution Flow Graph
                </span>
                <span className="text-[10px] text-slate-500 hidden sm:inline">
                  (Click any node to select and inspect)
                </span>
              </div>

              <div className="flex items-center gap-1.5 bg-slate-800/90 p-1 rounded-lg border border-slate-700 shadow-sm">
                <button
                  type="button"
                  onClick={handleZoomOut}
                  className="px-2.5 py-0.5 text-xs font-bold text-slate-300 hover:text-white hover:bg-slate-700 rounded transition-colors"
                  title="Zoom Out"
                >
                  -
                </button>
                <button
                  type="button"
                  onClick={handleZoomReset}
                  className="px-2 py-0.5 text-[11px] font-mono font-semibold text-blue-300 hover:text-white hover:bg-slate-700 rounded transition-colors"
                  title="Reset Zoom (100%)"
                >
                  {Math.round(scale * 100)}%
                </button>
                <button
                  type="button"
                  onClick={handleZoomIn}
                  className="px-2.5 py-0.5 text-xs font-bold text-slate-300 hover:text-white hover:bg-slate-700 rounded transition-colors"
                  title="Zoom In"
                >
                  +
                </button>
              </div>
            </div>

            {/* Flow Canvas Area */}
            <div
              ref={flowContainerRef}
              className="flex-1 overflow-auto devops-trace-scroll p-6 flex flex-col items-center justify-start bg-grid-pattern"
            >
              <div
                style={{
                  transform: `scale(${scale})`,
                  transformOrigin: "top center",
                  transition: "transform 0.15s ease-out",
                }}
                className="flex flex-col items-center w-full max-w-2xl py-3 space-y-1"
              >
                {renderedFlowGroups.length === 0 ? (
                  <div className="text-center py-16 text-slate-500 text-sm">
                    No execution steps recorded.
                  </div>
                ) : (
                  renderedFlowGroups.map((group, gIdx) => {
                    return (
                      <React.Fragment key={group.id}>
                        {gIdx > 0 && <FlowArrow />}

                        {group.type === "single" ? (
                          (() => {
                            const node = group.node;
                            const isSelected = selectedStepId === node.id;
                            const styles = getNodeStyles(node, isSelected);

                            return (
                              <div
                                onClick={() => setSelectedStepId(node.id)}
                                style={styles.style}
                                className={`w-full max-w-lg p-3.5 rounded-xl border transition-all cursor-pointer shadow-md hover:scale-[1.01] ${
                                  isSelected ? "ring-2 ring-blue-400 shadow-2xl" : ""
                                }`}
                              >
                                <div className="flex items-center justify-between gap-2.5">
                                  <div className="flex items-center gap-2.5 min-w-0">
                                    <span className={`text-base shrink-0 ${styles.iconColor}`}>
                                      <CategoryIcon category={node.category} stage={node.stage} />
                                    </span>
                                    <div className="min-w-0">
                                      <div
                                        style={{ color: styles.titleColor }}
                                        className="font-bold text-xs sm:text-sm truncate"
                                      >
                                        {node.title}
                                      </div>
                                      {node.subtitle && (
                                        <div
                                          style={{ color: styles.subtitleColor }}
                                          className="text-xs truncate mt-0.5"
                                        >
                                          {node.subtitle}
                                        </div>
                                      )}
                                    </div>
                                  </div>
                                  <div className="flex items-center gap-2 shrink-0">
                                    {node.duration_seconds !== null && node.duration_seconds !== undefined && (
                                      <span
                                        style={styles.durationStyle}
                                        className="font-mono text-xs px-2 py-0.5 rounded border border-white/10"
                                      >
                                        {Number(node.duration_seconds).toFixed(1)}s
                                      </span>
                                    )}
                                    <StateBadge status={node.status} />
                                  </div>
                                </div>

                                {node.error && (
                                  <div className="mt-2.5 p-2 bg-red-950/70 border border-red-500/50 rounded-lg text-xs text-red-200">
                                    <strong>Diagnostic:</strong> {node.error}
                                  </div>
                                )}
                              </div>
                            );
                          })()
                        ) : (
                          <div className="w-full flex flex-col items-center">
                            <div className="text-[10px] uppercase font-bold tracking-wider text-sky-400 bg-sky-950/70 px-2.5 py-0.5 rounded-full border border-sky-500/40 mb-1.5 shadow-sm">
                              Parallel Tool Executions ({group.nodes.length})
                            </div>
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 w-full max-w-xl">
                              {group.nodes.map((node) => {
                                const isSelected = selectedStepId === node.id;
                                const styles = getNodeStyles(node, isSelected);

                                return (
                                  <div
                                    key={node.id}
                                    onClick={() => setSelectedStepId(node.id)}
                                    style={styles.style}
                                    className={`p-3 rounded-xl border transition-all cursor-pointer shadow-md hover:scale-[1.01] flex flex-col justify-between gap-1.5 ${
                                      isSelected ? "ring-2 ring-blue-400 shadow-xl" : ""
                                    }`}
                                  >
                                    <div className="flex items-center justify-between gap-1.5">
                                      <div className="flex items-center gap-1.5 min-w-0">
                                        <span className={`text-base shrink-0 ${styles.iconColor}`}>
                                          <CategoryIcon category={node.category} stage={node.stage} />
                                        </span>
                                        <span
                                          style={{ color: styles.titleColor }}
                                          className="font-bold text-xs truncate"
                                        >
                                          {node.title}
                                        </span>
                                      </div>
                                      <StateBadge status={node.status} />
                                    </div>
                                    {node.subtitle && (
                                      <div
                                        style={{ color: styles.subtitleColor }}
                                        className="text-[11px] truncate pl-5"
                                      >
                                        {node.subtitle}
                                      </div>
                                    )}
                                    <div className="flex items-center justify-between text-[10px] text-slate-400 pl-5 mt-1">
                                      {node.duration_seconds !== null && (
                                        <span
                                          style={styles.durationStyle}
                                          className="font-mono px-1.5 py-0.5 rounded"
                                        >
                                          {Number(node.duration_seconds).toFixed(1)}s
                                        </span>
                                      )}
                                    </div>
                                  </div>
                                );
                              })}
                            </div>
                          </div>
                        )}
                      </React.Fragment>
                    );
                  })
                )}
              </div>
            </div>
          </section>

          {/* Bottom Section: Execution Output & Details Inspector */}
          <section className="h-64 sm:h-72 flex flex-col bg-[#060a12] shrink-0 overflow-hidden border-t border-slate-800">
            {/* Output Inspector Tabs */}
            <div className="flex items-center justify-between px-5 py-2 bg-slate-900/80 border-b border-slate-800 shrink-0">
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => setOutputTab("details")}
                  className={`px-3 py-1 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
                    outputTab === "details"
                      ? "bg-blue-600/30 text-blue-300 border border-blue-500/60 shadow-sm"
                      : "text-slate-400 hover:text-slate-200"
                  }`}
                >
                  Step Details
                </button>
                <button
                  type="button"
                  onClick={() => setOutputTab("raw")}
                  className={`px-3 py-1 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
                    outputTab === "raw"
                      ? "bg-blue-600/30 text-blue-300 border border-blue-500/60 shadow-sm"
                      : "text-slate-400 hover:text-slate-200"
                  }`}
                >
                  Raw Output
                </button>
                <button
                  type="button"
                  onClick={() => setOutputTab("json")}
                  className={`px-3 py-1 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
                    outputTab === "json"
                      ? "bg-blue-600/30 text-blue-300 border border-blue-500/60 shadow-sm"
                      : "text-slate-400 hover:text-slate-200"
                  }`}
                >
                  JSON Payload
                </button>
                <button
                  type="button"
                  onClick={() => setOutputTab("logs")}
                  className={`px-3 py-1 rounded-lg text-xs font-semibold transition-all cursor-pointer ${
                    outputTab === "logs"
                      ? "bg-blue-600/30 text-blue-300 border border-blue-500/60 shadow-sm"
                      : "text-slate-400 hover:text-slate-200"
                  }`}
                >
                  Timeline & Logs ({timeline.length})
                </button>
              </div>

              <div className="flex items-center gap-2">
                {outputTab === "json" && (
                  <button
                    type="button"
                    onClick={handleCopyJson}
                    className="px-2.5 py-1 text-[11px] font-medium bg-slate-800 hover:bg-slate-700 border border-slate-600 text-slate-300 hover:text-white rounded-md transition-all flex items-center gap-1 cursor-pointer"
                  >
                    {copied ? "✓ Copied!" : "📋 Copy JSON"}
                  </button>
                )}
              </div>
            </div>

            {/* Output Inspector Content */}
            <div className="flex-1 p-4 overflow-y-auto devops-trace-scroll text-xs">
              {outputTab === "details" && (
                <div className="space-y-3">
                  {selectedNode ? (
                    <div className="space-y-3">
                      <div className="flex items-center justify-between p-3 rounded-xl bg-slate-900/80 border border-slate-800">
                        <div>
                          <div className="font-bold text-sm text-slate-100 flex items-center gap-2">
                            <span><CategoryIcon category={selectedNode.category} stage={selectedNode.stage} /></span>
                            <span>{selectedNode.title}</span>
                          </div>
                          <div className="text-slate-400 text-xs mt-0.5">{selectedNode.subtitle || "No additional description"}</div>
                        </div>
                        <StateBadge status={selectedNode.status} />
                      </div>

                      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5">
                        <div className="p-2.5 rounded-lg bg-slate-900/60 border border-slate-800">
                          <span className="text-[10px] text-slate-400 uppercase font-semibold">Stage</span>
                          <div className="font-mono text-slate-200 mt-0.5">{selectedNode.stage || "standard"}</div>
                        </div>
                        <div className="p-2.5 rounded-lg bg-slate-900/60 border border-slate-800">
                          <span className="text-[10px] text-slate-400 uppercase font-semibold">Category</span>
                          <div className="font-mono text-slate-200 mt-0.5">{selectedNode.category || "system"}</div>
                        </div>
                        <div className="p-2.5 rounded-lg bg-slate-900/60 border border-slate-800">
                          <span className="text-[10px] text-slate-400 uppercase font-semibold">Start Offset</span>
                          <div className="font-mono text-slate-200 mt-0.5">+{Number(selectedNode.started_seconds || 0).toFixed(2)}s</div>
                        </div>
                        <div className="p-2.5 rounded-lg bg-slate-900/60 border border-slate-800">
                          <span className="text-[10px] text-slate-400 uppercase font-semibold">Duration</span>
                          <div className="font-mono text-slate-200 mt-0.5">
                            {selectedNode.duration_seconds !== null && selectedNode.duration_seconds !== undefined
                              ? `${Number(selectedNode.duration_seconds).toFixed(2)}s`
                              : "N/A"}
                          </div>
                        </div>
                      </div>

                      {selectedNode.error && (
                        <div className="p-3 rounded-xl bg-red-950/40 border border-red-500/40 text-red-200 space-y-1">
                          <div className="font-semibold text-xs text-red-300">Failure Diagnostic Details</div>
                          <div className="font-mono text-xs">{selectedNode.error}</div>
                        </div>
                      )}

                      <div className="p-2.5 rounded-lg bg-slate-900/40 border border-slate-800/80 text-[11px] text-slate-400 flex items-center gap-2">
                        <span className="text-emerald-400">🛡️</span>
                        <span>Read-only policy enforced; all operations are non-destructive and bound to inspection limits.</span>
                      </div>
                    </div>
                  ) : (
                    <div className="text-slate-500">Select a step from the list or diagram to inspect.</div>
                  )}
                </div>
              )}

              {outputTab === "raw" && (
                <div className="space-y-2">
                  <div className="p-3 rounded-xl bg-slate-900/60 border border-slate-800 text-slate-300 font-mono text-xs whitespace-pre-wrap">
                    {selectedNode ? (
                      `Step Title: ${selectedNode.title}\nSubtitle: ${selectedNode.subtitle || "N/A"}\nStage: ${selectedNode.stage}\nStatus: ${selectedNode.status}\nElapsed Offset: +${Number(selectedNode.started_seconds || 0).toFixed(2)}s\nDuration: ${selectedNode.duration_seconds !== null ? Number(selectedNode.duration_seconds).toFixed(2) + "s" : "In progress / N/A"}${selectedNode.error ? "\nError: " + selectedNode.error : ""}`
                    ) : (
                      "No step selected."
                    )}
                  </div>
                </div>
              )}

              {outputTab === "json" && (
                <pre className="p-3.5 rounded-xl bg-black/70 border border-slate-800 text-[11px] font-mono text-emerald-300 overflow-x-auto whitespace-pre">
                  {JSON.stringify(selectedNode ? { selected_step: selectedNode, trace_summary: summary } : trace, null, 2)}
                </pre>
              )}

              {outputTab === "logs" && (
                <div className="space-y-1.5 font-mono text-xs">
                  {timeline.length === 0 ? (
                    <div className="text-slate-500">No log entries recorded.</div>
                  ) : (
                    timeline.map((item, idx) => (
                      <div
                        key={idx}
                        className="flex items-center justify-between p-2.5 rounded-lg bg-slate-900/60 border border-slate-800/80 hover:bg-slate-800/60 transition-colors"
                      >
                        <div className="flex items-center gap-2.5 min-w-0">
                          <span className="text-slate-400 shrink-0 font-semibold">{item.timestamp}</span>
                          <span className="text-slate-200 truncate">{item.title}</span>
                        </div>
                        <div className="flex items-center gap-2 shrink-0">
                          {item.duration_seconds !== null && item.duration_seconds !== undefined && (
                            <span className="text-[10px] text-slate-400 bg-slate-800 px-1.5 py-0.5 rounded">
                              {Number(item.duration_seconds).toFixed(1)}s
                            </span>
                          )}
                          <StateBadge status={item.status} />
                        </div>
                      </div>
                    ))
                  )}
                </div>
              )}
            </div>
          </section>
        </main>
      </div>
    </div>
  );
}

// Main Button Export
export default function ExecutionViewerButton() {
  const [isOpen, setIsOpen] = React.useState(false);

  const traceData = (props && props.nodes) ? props : ((props && props.trace) ? props.trace : null);
  const nodes = traceData?.nodes || [];
  const summary = traceData?.summary || {};

  React.useEffect(() => {
    const handleGlobalOpen = (e) => {
      if (e && e.detail) {
        if (e.detail === traceData || (!traceData && e.detail)) {
          setIsOpen(true);
        }
      }
    };
    window.addEventListener("devops:open-execution-viewer", handleGlobalOpen);
    return () => window.removeEventListener("devops:open-execution-viewer", handleGlobalOpen);
  }, [traceData]);

  const renderModal = () => {
    if (!isOpen || !traceData) return null;
    const modal = <ExecutionViewerModal trace={traceData} onClose={() => setIsOpen(false)} />;
    if (typeof window !== "undefined" && window.ReactDOM && typeof window.ReactDOM.createPortal === "function" && document.body) {
      return window.ReactDOM.createPortal(modal, document.body);
    }
    return modal;
  };

  return (
    <div className="my-2.5 inline-block">
      <button
        type="button"
        onClick={() => setIsOpen(true)}
        className="inline-flex items-center gap-2 px-3.5 py-2 rounded-xl bg-slate-800/90 hover:bg-slate-700 text-slate-100 hover:text-white border border-slate-700/80 hover:border-blue-500/60 text-xs font-semibold shadow-md transition-all cursor-pointer group"
        aria-label="View Execution Details"
      >
        <span className="text-blue-400 group-hover:scale-110 transition-transform">📊</span>
        <span>View Execution Details</span>
        <span className="text-[10px] font-mono text-slate-400 group-hover:text-slate-300 bg-slate-900/80 px-2 py-0.5 rounded-full border border-slate-700/50">
          {nodes.length} steps · {Number(summary.elapsed_seconds || 0).toFixed(1)}s
        </span>
      </button>

      {renderModal()}
    </div>
  );
}
