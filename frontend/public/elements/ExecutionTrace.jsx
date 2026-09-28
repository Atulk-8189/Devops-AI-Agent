function StateBadge({ status }) {
  if (status === "active") {
    return (
      <span
        style={{ backgroundColor: "rgba(59, 130, 246, 0.2)", borderColor: "#60a5fa", color: "#93c5fd" }}
        className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[10px] font-medium border shadow-sm animate-pulse"
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
        className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[10px] font-medium border"
      >
        <span className="font-bold text-emerald-400">✓</span> Done
      </span>
    );
  }
  if (status === "failed") {
    return (
      <span
        style={{ backgroundColor: "rgba(239, 68, 68, 0.18)", borderColor: "#ef4444", color: "#fca5a5" }}
        className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[10px] font-medium border"
      >
        <span className="font-bold text-red-400">✕</span> Failed
      </span>
    );
  }
  if (status === "stopped") {
    return (
      <span
        style={{ backgroundColor: "rgba(245, 158, 11, 0.18)", borderColor: "#f59e0b", color: "#fde68a" }}
        className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[10px] font-medium border"
      >
        <span className="text-amber-400">■</span> Bounded
      </span>
    );
  }
  if (status === "cancelled") {
    return (
      <span
        style={{ backgroundColor: "rgba(113, 113, 122, 0.18)", borderColor: "#71717a", color: "#d4d4d8" }}
        className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[10px] font-medium border"
      >
        <span>■</span> Cancelled
      </span>
    );
  }
  return (
    <span
      style={{ backgroundColor: "rgba(82, 82, 91, 0.15)", borderColor: "#52525b", color: "#a1a1aa" }}
      className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[10px] font-medium border"
    >
      <span>○</span> Waiting
    </span>
  );
}

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

function getNodeStyles(node) {
  // Active stage (Bright blue highlight with glow)
  if (node.status === "active") {
    return {
      style: {
        backgroundColor: "rgba(14, 165, 233, 0.22)",
        borderColor: "#38bdf8",
        boxShadow: "0 0 12px rgba(56, 189, 248, 0.3)",
      },
      titleColor: "#e0f2fe",
      subtitleColor: "#7dd3fc",
      iconColor: "text-sky-300",
      durationStyle: { backgroundColor: "rgba(12, 74, 110, 0.6)", color: "#bae6fd" },
      activeRing: "ring-1 ring-sky-400 animate-pulse",
    };
  }

  // Failed stage (Red)
  if (node.status === "failed") {
    return {
      style: {
        backgroundColor: "rgba(127, 29, 29, 0.35)",
        borderColor: "#ef4444",
        boxShadow: "0 0 8px rgba(239, 68, 68, 0.2)",
      },
      titleColor: "#fecaca",
      subtitleColor: "#fca5a5",
      iconColor: "text-red-400",
      durationStyle: { backgroundColor: "rgba(153, 27, 27, 0.5)", color: "#fee2e2" },
      activeRing: "",
    };
  }

  // Stopped stage (Amber)
  if (node.status === "stopped") {
    return {
      style: {
        backgroundColor: "rgba(120, 53, 15, 0.35)",
        borderColor: "#f59e0b",
      },
      titleColor: "#fef3c7",
      subtitleColor: "#fde68a",
      iconColor: "text-amber-400",
      durationStyle: { backgroundColor: "rgba(146, 64, 14, 0.5)", color: "#fef3c7" },
      activeRing: "",
    };
  }

  // Category specific styles:
  // 1. User Request & Initialize Investigation: Blue
  if (node.stage === "user_request" || node.stage === "init" || node.category === "user") {
    return {
      style: {
        backgroundColor: "rgba(30, 58, 138, 0.3)",
        borderColor: "#3b82f6",
      },
      titleColor: "#dbeafe",
      subtitleColor: "#93c5fd",
      iconColor: "text-blue-400",
      durationStyle: { backgroundColor: "rgba(30, 58, 138, 0.5)", color: "#bfdbfe" },
      activeRing: "",
    };
  }

  // 2. MCP Connections: Teal / Green
  if (node.stage === "mcp_start") {
    return {
      style: {
        backgroundColor: "rgba(19, 78, 74, 0.3)",
        borderColor: "#14b8a6",
      },
      titleColor: "#ccfbf1",
      subtitleColor: "#5eead4",
      iconColor: "text-teal-400",
      durationStyle: { backgroundColor: "rgba(19, 78, 74, 0.5)", color: "#99f6e4" },
      activeRing: "",
    };
  }

  // 3. LLM Request and Response: Green / Teal
  if (node.stage === "llm_request" || node.category === "llm") {
    return {
      style: {
        backgroundColor: "rgba(6, 78, 59, 0.3)",
        borderColor: "#10b981",
      },
      titleColor: "#d1fae5",
      subtitleColor: "#6ee7b7",
      iconColor: "text-emerald-400",
      durationStyle: { backgroundColor: "rgba(6, 78, 59, 0.5)", color: "#a7f3d0" },
      activeRing: "",
    };
  }

  // 4. MCP Tool Execution: Sky Blue
  if (node.stage === "tool_execution" || node.category === "tool") {
    return {
      style: {
        backgroundColor: "rgba(12, 74, 110, 0.3)",
        borderColor: "#0ea5e9",
      },
      titleColor: "#e0f2fe",
      subtitleColor: "#7dd3fc",
      iconColor: "text-sky-400",
      durationStyle: { backgroundColor: "rgba(12, 74, 110, 0.5)", color: "#bae6fd" },
      activeRing: "",
    };
  }

  // 5. Evidence Processing: Purple
  if (node.stage === "evidence") {
    return {
      style: {
        backgroundColor: "rgba(88, 28, 135, 0.3)",
        borderColor: "#a855f7",
      },
      titleColor: "#f3e8ff",
      subtitleColor: "#d8b4fe",
      iconColor: "text-purple-400",
      durationStyle: { backgroundColor: "rgba(88, 28, 135, 0.5)", color: "#e9d5ff" },
      activeRing: "",
    };
  }

  // 6. MCP Cleanup: Orange
  if (node.stage === "mcp_cleanup") {
    return {
      style: {
        backgroundColor: "rgba(124, 45, 18, 0.3)",
        borderColor: "#f97316",
      },
      titleColor: "#ffedd5",
      subtitleColor: "#fdba74",
      iconColor: "text-orange-400",
      durationStyle: { backgroundColor: "rgba(124, 45, 18, 0.5)", color: "#fed7aa" },
      activeRing: "",
    };
  }

  // 7. Terminal Completed: Green
  if (node.stage === "terminal") {
    return {
      style: {
        backgroundColor: "rgba(20, 83, 45, 0.38)",
        borderColor: "#22c55e",
      },
      titleColor: "#dcfce7",
      subtitleColor: "#86efac",
      iconColor: "text-green-400",
      durationStyle: { backgroundColor: "rgba(20, 83, 45, 0.5)", color: "#bbf7d0" },
      activeRing: "",
    };
  }

  // Default fallback
  return {
    style: {
      backgroundColor: "rgba(30, 41, 59, 0.4)",
      borderColor: "#475569",
    },
    titleColor: "#f1f5f9",
    subtitleColor: "#94a3b8",
    iconColor: "text-slate-400",
    durationStyle: { backgroundColor: "rgba(30, 41, 59, 0.6)", color: "#cbd5e1" },
    activeRing: "",
  };
}

function FlowConnector({ label }) {
  return (
    <div className="flex flex-col items-center my-0.5 py-0">
      <div className="w-0.5 h-2 bg-slate-700/80" />
      {label && <span className="text-[9px] text-slate-400 uppercase px-1">{label}</span>}
      <svg className="w-2.5 h-2.5 text-slate-500 -mt-0.5" fill="currentColor" viewBox="0 0 12 12">
        <path d="M6 8.5L2.5 4h7L6 8.5z" />
      </svg>
    </div>
  );
}

function NodeCard({ node }) {
  const theme = getNodeStyles(node);

  return (
    <div
      style={theme.style}
      className={`rounded-lg border px-2.5 py-1.5 text-xs transition-all ${theme.activeRing}`}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0 flex-1">
          <span className={`text-xs shrink-0 ${theme.iconColor}`} aria-hidden="true">
            <CategoryIcon category={node.category} stage={node.stage} />
          </span>
          <div className="min-w-0 flex-1">
            <div style={{ color: theme.titleColor }} className="text-[11px] font-medium truncate leading-tight">
              {node.title}
            </div>
            {node.subtitle && (
              <div style={{ color: theme.subtitleColor }} className="text-[10px] truncate leading-none mt-0.5">
                {node.subtitle}
              </div>
            )}
          </div>
        </div>
        <div className="shrink-0 flex items-center gap-1">
          {node.duration_seconds !== null && node.duration_seconds !== undefined && (
            <span
              style={theme.durationStyle}
              className="text-[9px] font-mono px-1 py-0.5 rounded leading-none border border-white/10"
            >
              {Number(node.duration_seconds).toFixed(1)}s
            </span>
          )}
          <StateBadge status={node.status} />
        </div>
      </div>
      {node.error && (
        <div
          style={{ backgroundColor: "rgba(127, 29, 29, 0.7)", borderColor: "#ef4444", color: "#fecaca" }}
          className="mt-1 text-[10px] border rounded px-1.5 py-0.5 leading-tight"
        >
          {node.error}
        </div>
      )}
    </div>
  );
}

function FlowDiagramTab({ nodes }) {
  if (!nodes || nodes.length === 0) {
    return (
      <div className="text-center py-6 text-slate-400 text-xs">
        No execution trace steps recorded yet.
      </div>
    );
  }

  // Group parallel nodes together
  const renderedGroups = [];
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
      renderedGroups.push({ type: "parallel", id: gid, nodes: groupNodes });
    } else {
      renderedGroups.push({ type: "single", id: node.id, node: node });
      i++;
    }
  }

  return (
    <div className="space-y-0 py-1">
      {renderedGroups.map((group, idx) => (
        <div key={group.id || idx}>
          {idx > 0 && <FlowConnector />}
          {group.type === "single" ? (
            <NodeCard node={group.node} />
          ) : (
            <div
              style={{ backgroundColor: "rgba(12, 74, 110, 0.2)", borderColor: "#0ea5e9" }}
              className="border rounded-lg p-1.5 shadow-sm"
            >
              <div className="flex items-center justify-between mb-1 px-1">
                <span className="text-[9px] font-semibold text-sky-400 uppercase tracking-wider flex items-center gap-1">
                  <span>⚡</span> Parallel ({group.nodes.length} tools)
                </span>
                <span className="text-[9px] text-sky-300/80 font-mono">Concurrent</span>
              </div>
              <div className="grid grid-cols-1 gap-1">
                {group.nodes.map((pnode) => (
                  <NodeCard key={pnode.id} node={pnode} />
                ))}
              </div>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function TimelineTab({ timeline }) {
  if (!timeline || timeline.length === 0) {
    return (
      <div className="text-center py-6 text-slate-400 text-xs">
        No timeline events recorded yet.
      </div>
    );
  }

  return (
    <div className="relative pl-3 space-y-2 py-1 before:absolute before:left-1 before:top-2 before:bottom-2 before:w-0.5 before:bg-slate-800">
      {timeline.map((item) => (
        <div key={item.id} className="relative flex items-start gap-2 text-xs">
          <span className="absolute -left-3 top-1 h-2 w-2 rounded-full border border-slate-900 bg-slate-400 shrink-0" />
          <div
            style={{ backgroundColor: "rgba(15, 23, 42, 0.7)", borderColor: "rgba(51, 65, 85, 0.7)" }}
            className="flex-1 min-w-0 border rounded-lg px-2 py-1.5"
          >
            <div className="flex items-center justify-between gap-1 mb-0.5">
              <span className="font-mono text-[9px] text-slate-400">{item.timestamp}</span>
              <StateBadge status={item.status} />
            </div>
            <div className="font-medium text-slate-200 text-[11px] leading-tight">{item.title}</div>
            {item.duration_seconds !== null && item.duration_seconds !== undefined && (
              <div className="text-[9px] text-slate-400 font-mono mt-0.5">
                Duration: {Number(item.duration_seconds).toFixed(2)}s
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

function DetailsTab({ summary, details, metadata, connections }) {
  const llmCalls = details?.llm_calls || [];
  const toolCalls = details?.tool_calls || [];
  const lifecycle = details?.lifecycle || {};
  const meta = metadata || {};
  const agentDetails = meta.agent_reported_details || {};

  const target = meta.target || agentDetails.target || null;
  const coverage = meta.coverage || null;
  const clusterScope = agentDetails.cluster_scope || null;
  const namespaces = agentDetails.namespaces || agentDetails.namespace || null;
  const missingEvidence = Array.isArray(agentDetails.missing_evidence) ? agentDetails.missing_evidence : [];
  const limitations = Array.isArray(agentDetails.limitations) ? agentDetails.limitations : (agentDetails.limitations ? [agentDetails.limitations] : []);

  const hasScopeOrEvidence = target || (coverage && coverage !== "Not reported") || clusterScope || namespaces || missingEvidence.length > 0 || limitations.length > 0;

  return (
    <div className="space-y-2 py-1 text-xs">
      {/* Overview Card */}
      <div
        style={{ backgroundColor: "rgba(15, 23, 42, 0.6)", borderColor: "#334155" }}
        className="rounded-lg border p-2 space-y-1.5"
      >
        <div className="font-semibold text-slate-200 text-xs border-b border-slate-800 pb-1">Investigation Overview</div>
        <div className="grid grid-cols-2 gap-1.5 text-[10px]">
          <div>
            <span className="text-slate-400">Category:</span>{" "}
            <span className="font-medium text-slate-200">{summary?.category || meta.category || "Other"}</span>
          </div>
          <div>
            <span className="text-slate-400">Access Mode:</span>{" "}
            <span className="font-medium text-emerald-400">{meta.access_mode || "Read-only"}</span>
          </div>
          <div>
            <span className="text-slate-400">Elapsed:</span>{" "}
            <span className="font-mono text-slate-200">{Number(summary?.elapsed_seconds || 0).toFixed(1)}s</span>
          </div>
          <div>
            <span className="text-slate-400">Outcome:</span>{" "}
            <span className="font-medium capitalize text-slate-200">{summary?.outcome || "active"}</span>
          </div>
        </div>
      </div>

      {/* Scope & Evidence Card */}
      {hasScopeOrEvidence && (
        <div
          style={{ backgroundColor: "rgba(88, 28, 135, 0.15)", borderColor: "rgba(168, 85, 247, 0.35)" }}
          className="rounded-lg border p-2 space-y-1.5"
        >
          <div className="font-semibold text-purple-200 text-xs border-b border-purple-900/40 pb-1 flex items-center gap-1">
            <span>🔍</span> Scope & Evidence
          </div>
          <div className="space-y-1 text-[10px]">
            {target && (
              <div className="flex justify-between items-start gap-1">
                <span className="text-slate-400 shrink-0">Target:</span>
                <span className="font-mono text-slate-200 text-right break-all">{target}</span>
              </div>
            )}
            {clusterScope && typeof clusterScope === "object" && (
              <div className="space-y-0.5 pt-0.5 border-t border-purple-900/20">
                <div className="text-[9px] font-semibold text-purple-300 uppercase">Cluster Scope</div>
                {Object.entries(clusterScope).map(([k, v]) => (
                  <div key={k} className="flex justify-between items-start gap-1 pl-1">
                    <span className="text-slate-400 capitalize">{k.replace(/_/g, " ")}:</span>
                    <span className="font-mono text-slate-200 text-right">{String(v)}</span>
                  </div>
                ))}
              </div>
            )}
            {namespaces && (
              <div className="flex justify-between items-start gap-1">
                <span className="text-slate-400 shrink-0">Namespaces:</span>
                <span className="font-mono text-slate-200 text-right">{Array.isArray(namespaces) ? namespaces.join(", ") : String(namespaces)}</span>
              </div>
            )}
            {coverage && coverage !== "Not reported" && (
              <div className="flex justify-between items-start gap-1">
                <span className="text-slate-400 shrink-0">Coverage:</span>
                <span className="text-amber-300 font-medium">{coverage}</span>
              </div>
            )}
            {missingEvidence.length > 0 && (
              <div className="pt-0.5 border-t border-purple-900/20">
                <span className="text-slate-400">Missing Evidence:</span>
                <ul className="list-disc list-inside text-slate-300 mt-0.5 space-y-0.5 pl-1">
                  {missingEvidence.map((item, idx) => (
                    <li key={idx} className="text-[9px]">{item}</li>
                  ))}
                </ul>
              </div>
            )}
            {limitations.length > 0 && (
              <div className="pt-0.5 border-t border-purple-900/20">
                <span className="text-slate-400">Limitations:</span>
                <ul className="list-disc list-inside text-slate-300 mt-0.5 space-y-0.5 pl-1">
                  {limitations.map((item, idx) => (
                    <li key={idx} className="text-[9px]">{item}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </div>
      )}

      {/* LLM Iterations */}
      <div
        style={{ backgroundColor: "rgba(6, 78, 59, 0.2)", borderColor: "rgba(16, 185, 129, 0.4)" }}
        className="rounded-lg border p-2 space-y-1.5"
      >
        <div className="flex items-center justify-between border-b border-emerald-900/40 pb-1">
          <div className="font-semibold text-emerald-200 text-xs flex items-center gap-1">
            <span>🧠</span> LLM Requests ({llmCalls.length})
          </div>
        </div>
        {llmCalls.length === 0 ? (
          <div className="text-slate-400 text-[10px]">No model calls recorded.</div>
        ) : (
          <div className="space-y-1">
            {llmCalls.map((c) => (
              <div
                key={c.sequence}
                style={{ backgroundColor: "rgba(2, 44, 34, 0.5)", borderColor: "rgba(16, 185, 129, 0.3)" }}
                className="border rounded px-2 py-1 flex items-center justify-between gap-1.5"
              >
                <div>
                  <div className="font-medium text-[11px] text-emerald-100">Iteration #{c.sequence}</div>
                  <div className="text-[9px] text-emerald-300/70 font-mono">
                    +{Number(c.started_seconds).toFixed(1)}s
                    {c.duration_seconds !== null && ` · ${Number(c.duration_seconds).toFixed(1)}s`}
                  </div>
                  {c.error && <div className="text-[9px] text-red-400 mt-0.5">{c.error}</div>}
                </div>
                <StateBadge status={c.status} />
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Tool Executions */}
      <div
        style={{ backgroundColor: "rgba(12, 74, 110, 0.2)", borderColor: "rgba(14, 165, 233, 0.4)" }}
        className="rounded-lg border p-2 space-y-1.5"
      >
        <div className="flex items-center justify-between border-b border-sky-900/40 pb-1">
          <div className="font-semibold text-sky-200 text-xs flex items-center gap-1">
            <span>⚡</span> Tool Executions ({toolCalls.length})
          </div>
        </div>
        {toolCalls.length === 0 ? (
          <div className="text-slate-400 text-[10px]">No tool dispatches recorded.</div>
        ) : (
          <div className="space-y-1">
            {toolCalls.map((t, idx) => (
              <div
                key={idx}
                style={{ backgroundColor: "rgba(8, 47, 73, 0.5)", borderColor: "rgba(14, 165, 233, 0.3)" }}
                className="border rounded px-2 py-1 flex items-start justify-between gap-1.5"
              >
                <div className="min-w-0">
                  <div className="font-mono text-[11px] font-semibold text-sky-200 truncate leading-tight">{t.tool_name}</div>
                  <div className="text-[9px] text-sky-300/70 leading-tight mt-0.5">{t.purpose}</div>
                  <div className="text-[9px] text-sky-300/60 font-mono mt-0.5">
                    +{Number(t.started_seconds).toFixed(1)}s
                    {t.duration_seconds !== null && ` · ${Number(t.duration_seconds).toFixed(1)}s`}
                  </div>
                  {t.error && <div className="text-[9px] text-red-400 mt-0.5">{t.error}</div>}
                </div>
                <StateBadge status={t.status} />
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Lifecycle & Safety */}
      <div
        style={{ backgroundColor: "rgba(15, 23, 42, 0.6)", borderColor: "#334155" }}
        className="rounded-lg border p-2 space-y-1"
      >
        <div className="font-semibold text-slate-200 text-xs border-b border-slate-800 pb-1 flex items-center gap-1">
          <span>🔌</span> Lifecycle & Security
        </div>
        <div className="space-y-1 text-[10px]">
          <div className="flex justify-between">
            <span className="text-slate-400">MCP Startup:</span>
            <span className="font-mono text-slate-200">
              {lifecycle.mcp_startup_duration_seconds !== null ? `${Number(lifecycle.mcp_startup_duration_seconds).toFixed(1)}s` : "Ready"}
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-slate-400">MCP Cleanup:</span>
            <span className="capitalize text-slate-200">{lifecycle.mcp_cleanup_status || "Not initiated"}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-slate-400">Safety Boundary:</span>
            <span className="text-slate-200">{lifecycle.safety_bounded ? "Bounded by budget" : "Within bounds"}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function ConnectionBadge({ name, status, label }) {
  // Default: Unknown (Neutral gray)
  let dotColor = "#71717a"; // zinc-500
  let textColor = "#a1a1aa"; // zinc-400
  let badgeStyle = {
    backgroundColor: "rgba(39, 39, 42, 0.5)",
    borderColor: "rgba(82, 82, 91, 0.5)",
  };

  if (status === "ready" || status === "connected") {
    // Connected / Ready: Green
    dotColor = "#34d399"; // emerald-400
    textColor = "#6ee7b7"; // emerald-300
    badgeStyle = {
      backgroundColor: "rgba(6, 78, 59, 0.35)",
      borderColor: "#10b981",
    };
  } else if (status === "connecting" || status === "active") {
    // Connecting: Yellow / Amber
    dotColor = "#fbbf24"; // amber-400
    textColor = "#fde68a"; // amber-300
    badgeStyle = {
      backgroundColor: "rgba(120, 53, 15, 0.35)",
      borderColor: "#f59e0b",
    };
  } else if (status === "stopped" || status === "closed") {
    // Stopped (Normal cleanup after execution): Neutral gray
    dotColor = "#94a3b8"; // slate-400
    textColor = "#cbd5e1"; // slate-300
    badgeStyle = {
      backgroundColor: "rgba(30, 41, 59, 0.5)",
      borderColor: "rgba(71, 85, 105, 0.6)",
    };
  } else if (status === "failed") {
    // Failed / Auth Failed / Denied: Red
    dotColor = "#f87171"; // red-400
    textColor = "#fca5a5"; // red-300
    badgeStyle = {
      backgroundColor: "rgba(127, 29, 29, 0.35)",
      borderColor: "#ef4444",
    };
  }

  return (
    <div
      style={badgeStyle}
      className="flex items-center justify-between px-2 py-1 rounded-md border text-[10px] min-w-0 transition-colors shadow-sm gap-1 overflow-hidden"
    >
      <div className="flex items-center gap-1.5 min-w-0 flex-1 overflow-hidden">
        <span
          style={{ backgroundColor: dotColor }}
          className={`h-1.5 w-1.5 rounded-full shrink-0 ${status === "connecting" ? "animate-pulse" : ""}`}
        />
        <span className="text-slate-200 font-medium truncate leading-tight">{name}</span>
      </div>
      <span
        style={{ color: textColor }}
        className="font-mono text-[9px] font-semibold shrink-0 ml-1 px-1 py-0.5 rounded bg-black/30 leading-tight"
      >
        {label}
      </span>
    </div>
  );
}

function ConnectionHeader({ connections }) {
  const list = [
    connections?.azure || { name: "Azure", status: "unknown", label: "Unknown" },
    connections?.azure_devops || { name: "Azure DevOps", status: "unknown", label: "Unknown" },
    connections?.aks || { name: "AKS", status: "unknown", label: "Unknown" },
    connections?.mcp || { name: "MCP", status: "unknown", label: "Unknown" },
  ];

  return (
    <div className="grid grid-cols-2 gap-1.5 my-1.5 w-full" aria-label="Connection Status Indicators">
      {list.map((c) => (
        <ConnectionBadge key={c.name} name={c.name} status={c.status} label={c.label} />
      ))}
    </div>
  );
}

export default function ExecutionTrace() {
  const store = typeof window !== "undefined" ? window.__DEVOPS_TRACE_STORE__ : null;
  const [activeTab, setActiveTab] = React.useState(() => {
    if (store && store.activeTab) return store.activeTab;
    if (typeof localStorage !== "undefined") {
      return localStorage.getItem("devops_sidebar_tab") || "flow";
    }
    return "flow";
  });

  // Sync incoming props into store
  React.useEffect(() => {
    if (props && store) {
      store.updateTrace(props);
    }
  }, [props, store]);

  // Subscribe to store updates for tab changes
  React.useEffect(() => {
    if (!store) return;
    const unsubscribe = store.subscribe((s) => {
      if (s.activeTab) setActiveTab(s.activeTab);
    });
    return unsubscribe;
  }, [store]);

  const handleTabChange = (tab) => {
    setActiveTab(tab);
    if (store) store.setActiveTab(tab);
    try {
      localStorage.setItem("devops_sidebar_tab", tab);
    } catch (e) {}
  };

  const handleCollapse = () => window.__DEVOPS_COLLAPSE_SIDEBAR__?.();

  const traceData = (props && props.nodes) ? props : ((store && store.currentTrace) ? store.currentTrace : (props || {}));
  const nodes = traceData?.nodes || [];
  const timeline = traceData?.timeline || [];
  const summary = traceData?.summary || {};
  const details = traceData?.details || {};
  const metadata = traceData?.metadata || {};
  const connections = traceData?.connections || {};

  return (
    <div
      id="devops-execution-trace-panel"
      className="relative w-full h-full flex flex-col font-sans select-none text-slate-200 p-2 space-y-2 overflow-hidden bg-[#0b0f19] rounded-xl border border-slate-700/60"
      aria-label="Investigation Details Sidebar"
    >
      {/* Header */}
      <div className="flex items-center justify-between gap-1.5 border-b border-slate-800 pb-1.5 shrink-0">
        <div className="flex items-center gap-1.5 min-w-0 flex-1">
          <span className="text-sm" aria-hidden="true">📊</span>
          <div className="min-w-0 flex-1">
            <h2 className="text-xs font-semibold text-slate-100 leading-tight">Execution Trace</h2>
            <div className="text-[9px] text-slate-400 flex items-center gap-1 mt-0.5 truncate">
              <span className="truncate">{summary.category || "Investigation"}</span>
              <span>·</span>
              <span className="text-emerald-400 shrink-0 font-medium">Read-only</span>
              <span>·</span>
              <span className="font-mono shrink-0 text-slate-300">{Number(summary.elapsed_seconds || 0).toFixed(1)}s</span>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <StateBadge status={summary.outcome || "active"} />
          <button
            type="button"
            onClick={() => {
              if (typeof window !== "undefined" && window.__DEVOPS_OPEN_EXECUTION_VIEWER__) {
                window.__DEVOPS_OPEN_EXECUTION_VIEWER__(traceData);
              }
            }}
            aria-label="Open Full-Screen Execution Viewer"
            title="Open Full-Screen Execution Viewer"
            style={{ backgroundColor: "rgba(30, 41, 59, 0.7)", borderColor: "#475569" }}
            className="p-1 rounded-md border text-blue-300 hover:text-white hover:bg-slate-700 transition-colors cursor-pointer"
          >
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5l-5-5m5 5v-4m0 4h-4" />
            </svg>
          </button>
          <button
            type="button"
            onClick={handleCollapse}
            aria-label="Collapse Investigation Details"
            title="Collapse Investigation Details"
            style={{ backgroundColor: "rgba(30, 41, 59, 0.7)", borderColor: "#475569" }}
            className="p-1 rounded-md border text-slate-300 hover:text-white hover:bg-slate-700 transition-colors cursor-pointer"
          >
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
            </svg>
          </button>
        </div>
      </div>

      {/* Connection Status Badges */}
      <div className="shrink-0">
        <ConnectionHeader connections={connections} />
      </div>

      {/* Navigation Tabs */}
      <nav
        style={{ backgroundColor: "rgba(15, 23, 42, 0.8)", borderColor: "#334155" }}
        className="flex rounded-md border p-0.5 text-xs font-medium shrink-0"
        aria-label="Trace views"
      >
        <button
          type="button"
          onClick={() => handleTabChange("flow")}
          style={activeTab === "flow" ? { backgroundColor: "rgba(30, 41, 59, 0.9)", color: "#93c5fd", borderColor: "rgba(59, 130, 246, 0.4)" } : {}}
          className={`flex-1 rounded py-0.5 text-center text-[10px] transition-all ${
            activeTab === "flow"
              ? "shadow-sm font-semibold border"
              : "text-slate-400 hover:text-slate-200"
          }`}
        >
          Flow Diagram
        </button>
        <button
          type="button"
          onClick={() => handleTabChange("timeline")}
          style={activeTab === "timeline" ? { backgroundColor: "rgba(30, 41, 59, 0.9)", color: "#93c5fd", borderColor: "rgba(59, 130, 246, 0.4)" } : {}}
          className={`flex-1 rounded py-0.5 text-center text-[10px] transition-all ${
            activeTab === "timeline"
              ? "shadow-sm font-semibold border"
              : "text-slate-400 hover:text-slate-200"
          }`}
        >
          Timeline
        </button>
        <button
          type="button"
          onClick={() => handleTabChange("details")}
          style={activeTab === "details" ? { backgroundColor: "rgba(30, 41, 59, 0.9)", color: "#93c5fd", borderColor: "rgba(59, 130, 246, 0.4)" } : {}}
          className={`flex-1 rounded py-0.5 text-center text-[10px] transition-all ${
            activeTab === "details"
              ? "shadow-sm font-semibold border"
              : "text-slate-400 hover:text-slate-200"
          }`}
        >
          Details
        </button>
      </nav>

      {/* Content Area */}
      <div className="overflow-y-auto devops-trace-scroll flex-1 pr-1 space-y-2">
        {activeTab === "flow" && <FlowDiagramTab nodes={nodes} />}
        {activeTab === "timeline" && <TimelineTab timeline={timeline} />}
        {activeTab === "details" && <DetailsTab summary={summary} details={details} metadata={metadata} connections={connections} />}
      </div>
    </div>
  );
}
