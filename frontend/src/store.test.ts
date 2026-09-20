import { describe, expect, it, beforeEach } from "vitest";
import { useWorkspace } from "./store";
describe("workspace state", () => {
  beforeEach(() =>
    useWorkspace.setState({
      conversationId: undefined,
      selectedAttachment: undefined,
      selectedObservation: undefined,
      canvasMode: "map",
      mobile: "chat",
      follow: true,
      nav: true,
      drawer: false,
      drafts: [],
      references: [],
      secondAttachment: undefined,
      comparisonSlider: false,
      compareSplit: 50,
      canvasView: undefined,
    }),
  );
  it("keeps observation and canvas focus together", () => {
    useWorkspace
      .getState()
      .set({
        selectedObservation: "obs-1",
        selectedAttachment: "att-1",
        canvasMode: "image",
        canvasView: {
          x: 0.4,
          y: 0.6,
          scale: 0.01,
          rotation: 0,
          source: "att-1",
        },
      });
    expect(useWorkspace.getState()).toMatchObject({
      selectedObservation: "obs-1",
      selectedAttachment: "att-1",
      canvasMode: "image",
      canvasView: { source: "att-1" },
    });
  });
  it("keeps compare controls in workspace state", () => {
    useWorkspace
      .getState()
      .set({
        canvasMode: "compare",
        secondAttachment: "att-2",
        comparisonSlider: true,
        compareSplit: 63,
      });
    expect(useWorkspace.getState()).toMatchObject({
      canvasMode: "compare",
      secondAttachment: "att-2",
      comparisonSlider: true,
      compareSplit: 63,
    });
  });
  it("clears canvas focus when a different conversation is selected", () => {
    useWorkspace
      .getState()
      .set({
        conversationId: "one",
        canvasMode: "image",
        selectedAttachment: "att-1",
        selectedObservation: "obs-1",
      });
    useWorkspace.getState().selectConversation("two");
    expect(useWorkspace.getState()).toMatchObject({
      conversationId: "two",
      canvasMode: "map",
      selectedAttachment: undefined,
      selectedObservation: undefined,
    });
  });
  it("accepts an active id changed by a state update", () => {
    useWorkspace.getState().set({ conversationId: "direct-id" });
    expect(useWorkspace.getState().conversationId).toBe("direct-id");
  });
  it("allows the agent follow mode to be changed", () => {
    useWorkspace.getState().set({ follow: false });
    expect(useWorkspace.getState().follow).toBe(false);
  });
});
