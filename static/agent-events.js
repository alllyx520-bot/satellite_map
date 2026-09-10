(function (root) {
  "use strict";
  class RunEventStore {
    constructor(runId) { this.runId = Number(runId); this.cursor = 0; this.pending = new Map(); }
    ingest(events) {
      for (const event of events) {
        if (event.schema_version !== 1 || Number(event.run_id) !== this.runId || !Number.isSafeInteger(event.sequence) || event.sequence <= this.cursor) continue;
        if (!this.pending.has(event.sequence)) this.pending.set(event.sequence, event);
      }
      const ready = [];
      while (this.pending.has(this.cursor + 1)) {
        this.cursor += 1;
        ready.push(this.pending.get(this.cursor));
        this.pending.delete(this.cursor);
      }
      return ready;
    }
    get hasGap() { return this.pending.size > 0 && !this.pending.has(this.cursor + 1); }
  }
  root.RunEventStore = RunEventStore;
  if (typeof module !== "undefined") module.exports = { RunEventStore };
})(typeof window === "undefined" ? globalThis : window);
