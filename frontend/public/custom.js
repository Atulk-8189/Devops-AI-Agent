/**
 * DevOps AI Agent - Persistent Investigation Details Sidebar & Lifecycle Manager
 */
(function () {
  if (typeof window === "undefined") return;

  const STORAGE_KEY_TAB = "devops_sidebar_tab";

  const defaultTrace = {
    nodes: [],
    edges: [],
    timeline: [],
    connections: {
      azure: { name: "Azure", status: "unknown", label: "Unknown" },
      azure_devops: { name: "Azure DevOps", status: "unknown", label: "Unknown" },
      aks: { name: "AKS", status: "unknown", label: "Unknown" },
      mcp: { name: "MCP", status: "unknown", label: "Unknown" },
    },
    summary: {
      category: "Ready",
      access_mode: "Read-only",
      outcome: "waiting",
      elapsed_seconds: 0.0,
      total_nodes: 0,
      model_call_count: 0,
      tool_call_count: 0,
      terminal: false,
    },
    details: {
      llm_calls: [],
      tool_calls: [],
      lifecycle: {},
    },
    metadata: {},
  };

  const store = {
    activeTab: (typeof localStorage !== "undefined" && localStorage.getItem(STORAGE_KEY_TAB)) || "flow",
    currentTrace: defaultTrace,
    tracesByThread: {},
    currentThreadId: null,
    hasEverRun: false,
    listeners: new Set(),

    subscribe(listener) {
      this.listeners.add(listener);
      return () => this.listeners.delete(listener);
    },

    notify() {
      for (const listener of this.listeners) {
        try {
          listener(this);
        } catch (e) {
          console.error("Trace store listener error:", e);
        }
      }
      updateRailUI();
    },

    setActiveTab(tab) {
      this.activeTab = tab;
      try {
        localStorage.setItem(STORAGE_KEY_TAB, tab);
      } catch (e) {}
      this.notify();
    },

    updateTrace(trace, threadId) {
      if (!trace) return;
      this.currentTrace = trace;
      this.hasEverRun = true;
      if (threadId) {
        this.currentThreadId = threadId;
        this.tracesByThread[threadId] = trace;
      }
      this.notify();
    },

    setThread(threadId) {
      this.currentThreadId = threadId;
      if (threadId && this.tracesByThread[threadId]) {
        this.currentTrace = this.tracesByThread[threadId];
      }
      this.notify();
    },
  };

  window.__DEVOPS_TRACE_STORE__ = store;

  // One controller owns the outer layout. React only renders trace content.
  let layout = null;
  let scheduled = false;
  const WIDTH_KEY = "devops_panel_width";
  const DEFAULT_WIDTH = 288; // Measured existing desktop default; do not change.

  function savedWidth() {
    try {
      const value = Number(localStorage.getItem(WIDTH_KEY));
      return Number.isFinite(value) && value >= 240 ? value : DEFAULT_WIDTH;
    } catch { return DEFAULT_WIDTH; }
  }

  function updateRailUI() {
    if (layout) {
      layout.rail.title = "Expand Execution Trace";
    }
  }

  window.__DEVOPS_COLLAPSE_SIDEBAR__ = () => {
    if (layout) {
      layout.stopDrag();
      layout.collapsed = true;
      layout.apply();
      layout.rail.focus();
    } else {
      // Chainlit uses a dialog on mobile, rather than a desktop split panel.
      const dialog = document.getElementById("devops-execution-trace-panel")?.closest('[role="dialog"]');
      const close = [...(dialog?.querySelectorAll("button") || [])].find(b => b.textContent.trim() === "Close");
      close?.click();
    }
  };

  window.__DEVOPS_REOPEN_SIDEBAR__ = () => {
    if (layout) {
      layout.collapsed = false;
      layout.apply();
      layout.handle.focus();
    }
  };

  window.__DEVOPS_OPEN_EXECUTION_VIEWER__ = trace => {
    window.dispatchEvent(new CustomEvent("devops:open-execution-viewer", {detail: trace || store.currentTrace}));
  };

  function mountLayout(group, panel, chat, nativeHandle) {
    const handle = document.createElement("div");
    handle.id = "devops-trace-splitter";
    handle.tabIndex = 0;
    handle.setAttribute("role", "separator");
    handle.setAttribute("aria-orientation", "vertical");
    handle.setAttribute("aria-label", "Resize Execution Trace");
    handle.title = "Drag to resize Execution Trace; use arrow keys when focused";
    const rail = document.createElement("button");
    rail.id = "devops-sidebar-rail";
    rail.type = "button";
    rail.textContent = "‹";
    rail.setAttribute("aria-label", "Expand Investigation Details");
    rail.addEventListener("click", window.__DEVOPS_REOPEN_SIDEBAR__);
    group.insertBefore(handle, panel);
    group.appendChild(rail);
    group.setAttribute("data-devops-trace-layout", "");
    panel.setAttribute("data-devops-trace-pane", "");
    chat.setAttribute("data-devops-chat-pane", "");
    nativeHandle?.setAttribute("data-devops-native-splitter", "");
    let drag = null;
    const state = {
      group, panel, chat, handle, rail, collapsed: false, width: savedWidth(),
      limits() {
        // Reserve 360px for the composer/chat and 6px for the divider.
        const max = Math.max(240, Math.min(800, group.clientWidth - 366));
        return {min: 240, max};
      },
      apply() {
        const {min, max} = this.limits();
        const rendered = Math.max(min, Math.min(max, this.width));
        const value = `${rendered}px`;
        if (group.style.getPropertyValue("--devops-panel-width") !== value) {
          group.style.setProperty("--devops-panel-width", value);
        }
        group.toggleAttribute("data-devops-trace-collapsed", this.collapsed);
        handle.setAttribute("aria-valuemin", min);
        handle.setAttribute("aria-valuemax", max);
        handle.setAttribute("aria-valuenow", Math.round(rendered));
        rail.hidden = !this.collapsed;
        handle.hidden = this.collapsed;
      },
      resize(value) {
        const {min, max} = this.limits();
        this.width = Math.max(min, Math.min(max, value));
        this.apply();
        try { localStorage.setItem(WIDTH_KEY, String(this.width)); } catch {}
      },
      stopDrag() {
        if (!drag) return;
        const pointer = drag.pointer;
        drag = null;
        if (handle.hasPointerCapture(pointer)) handle.releasePointerCapture(pointer);
        document.body.classList.remove("devops-trace-dragging");
      },
      destroy() {
        this.stopDrag();
        observer.disconnect();
        window.removeEventListener("blur", stop);
        document.removeEventListener("pointermove", move, true);
        document.removeEventListener("pointerup", stop, true);
        document.removeEventListener("pointercancel", stop, true);
        handle.remove(); rail.remove();
        group.removeAttribute("data-devops-trace-layout");
        group.removeAttribute("data-devops-trace-collapsed");
        panel.removeAttribute("data-devops-trace-pane");
        chat.removeAttribute("data-devops-chat-pane");
        nativeHandle?.removeAttribute("data-devops-native-splitter");
      },
    };
    handle.addEventListener("pointerdown", event => {
      if (event.button !== 0 || state.collapsed) return;
      event.preventDefault();
      event.stopPropagation();
      handle.focus();
      drag = {pointer: event.pointerId, x: event.clientX, width: panel.getBoundingClientRect().width};
      handle.setPointerCapture(event.pointerId);
      document.body.classList.add("devops-trace-dragging");
    });
    const move = event => {
      if (drag && drag.pointer === event.pointerId) state.resize(drag.width + drag.x - event.clientX);
    };
    document.addEventListener("pointermove", move, true);
    const stop = () => state.stopDrag();
    for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) handle.addEventListener(name, stop);
    window.addEventListener("blur", stop);
    document.addEventListener("pointerup", stop, true);
    document.addEventListener("pointercancel", stop, true);
    handle.addEventListener("keydown", event => {
      const change = {ArrowLeft: 24, ArrowRight: -24}[event.key];
      if (change) { event.preventDefault(); state.resize(panel.getBoundingClientRect().width + change); }
      if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        state.resize(state.limits()[event.key === "Home" ? "min" : "max"]);
      }
    });
    const observer = new ResizeObserver(() => state.apply());
    observer.observe(group);
    state.apply();
    return state;
  }

  function reconcile() {
    scheduled = false;
    const trace = document.getElementById("devops-execution-trace-panel");
    const panel = trace?.closest("[data-panel]");
    const group = panel?.parentElement;
    if (layout && (layout.panel !== panel || !panel?.isConnected)) {
      layout.destroy();
      layout = null;
    }
    if (!layout && group?.hasAttribute("data-panel-group")) {
      const children = [...group.children];
      const chat = children.find(e => e !== panel && e.hasAttribute("data-panel"));
      const nativeHandle = children.find(e => e.hasAttribute("data-panel-resize-handle-id"));
      if (chat) layout = mountLayout(group, panel, chat, nativeHandle);
    }
  }

  function schedule() {
    if (!scheduled) { scheduled = true; requestAnimationFrame(reconcile); }
  }

  function setup() {
    new MutationObserver(schedule).observe(document.getElementById("root") || document.body, {childList: true, subtree: true});
    schedule();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", setup);
  else setup();
})();
