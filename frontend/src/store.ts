import { create } from "zustand";
import type { Attachment } from "./types";
import type { ViewState } from "./ImageCanvas";

type CanvasMode = "map" | "image" | "compare";
type MobilePane = "chat" | "canvas";
type SavedWorkspace = {
  selectedAttachment?: string;
  selectedObservation?: string;
  canvasMode: CanvasMode;
  mobile: MobilePane;
  follow: boolean;
  drafts: Attachment[];
  references: string[];
  secondAttachment?: string;
  comparisonSlider: boolean;
  compareSplit: number;
  canvasView?: ViewState;
  selectedEvidence?: string;
  mapTarget?: number[];
  mapView?: {center:number[]; zoom:number};
};
type State = SavedWorkspace & {
  conversationId?: string;
  nav: boolean;
  drawer: boolean;
  set: (v: Partial<State>) => void;
  addDraft: (x: Attachment) => void;
  removeDraft: (id: string) => void;
  selectConversation: (id?: string) => void;
};
const activeKey = "satellitesense:conversation";
const stateKey = (id: string) => "satellitesense:workspace:" + id;
const storage =
  typeof window === "undefined" ? undefined : window.sessionStorage;
const defaults = (): SavedWorkspace => ({
  selectedAttachment: undefined,
  selectedObservation: undefined,
  canvasMode: "map",
  mobile: "chat",
  follow: false,
  drafts: [],
  references: [],
  secondAttachment: undefined,
  comparisonSlider: false,
  compareSplit: 50,
  canvasView: undefined,
  selectedEvidence: undefined,
  mapTarget: undefined,
  mapView: undefined,
});
function restored(id?: string): SavedWorkspace {
  if (!id) return defaults();
  try {
    const value = JSON.parse(storage?.getItem(stateKey(id)) || "{}");
    return {
      ...defaults(),
      ...value,
      canvasMode: ["map", "image", "compare"].includes(value.canvasMode)
        ? value.canvasMode
        : "map",
      mobile: value.mobile === "canvas" ? "canvas" : "chat",
      compareSplit: Math.max(
        0,
        Math.min(100, Number(value.compareSplit) || 50),
      ),
    };
  } catch {
    return defaults();
  }
}
function snapshot(s: State): SavedWorkspace {
  return {
    selectedAttachment: s.selectedAttachment,
    selectedObservation: s.selectedObservation,
    canvasMode: s.canvasMode,
    mobile: s.mobile,
    follow: s.follow,
    drafts: s.drafts,
    references: s.references,
    secondAttachment: s.secondAttachment,
    comparisonSlider: s.comparisonSlider,
    compareSplit: s.compareSplit,
    canvasView: s.canvasView,
    selectedEvidence: s.selectedEvidence,
    mapTarget: s.mapTarget,
    mapView: s.mapView,
  };
}
const initialId = storage?.getItem(activeKey) || undefined;
export const useWorkspace = create<State>((set, get) => ({
  conversationId: initialId,
  ...restored(initialId),
  nav: typeof window !== "undefined" && window.innerWidth > 1000,
  drawer: false,
  set: (v) =>
    set((s) => ({
      ...s,
      ...(v.mobile && typeof window !== "undefined" && window.innerWidth < 800
        ? { nav: false }
        : {}),
      ...v,
    })),
  addDraft: (x) =>
    set((s) => ({ drafts: [...s.drafts.filter((a) => a.id !== x.id), x] })),
  removeDraft: (id) =>
    set((s) => ({ drafts: s.drafts.filter((x) => x.id !== id) })),
  selectConversation: (id) => {
    const current = get();
    if (current.conversationId)
      storage?.setItem(
        stateKey(current.conversationId),
        JSON.stringify(snapshot(current)),
      );
    if (id) storage?.setItem(activeKey, id);
    else storage?.removeItem(activeKey);
    set({ conversationId: id, ...restored(id), drawer: false });
  },
}));
useWorkspace.subscribe((state, previous) => {
  if (state.conversationId !== previous.conversationId) {
    if (previous.conversationId)
      storage?.setItem(
        stateKey(previous.conversationId),
        JSON.stringify(snapshot(previous)),
      );
    if (state.conversationId) storage?.setItem(activeKey, state.conversationId);
    else storage?.removeItem(activeKey);
    return;
  }
  if (state.conversationId)
    storage?.setItem(
      stateKey(state.conversationId),
      JSON.stringify(snapshot(state)),
    );
});
if (typeof window !== "undefined") {
  const narrow = window.matchMedia("(max-width:800px)");
  narrow.addEventListener("change", () => {
    if (narrow.matches) useWorkspace.getState().set({ nav: false });
  });
}
