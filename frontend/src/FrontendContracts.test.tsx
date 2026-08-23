import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { advanceCursor, CursorPagination, retreatCursor } from "./Pagination";
import { RunStateEvidence } from "./RunsIndex";

function stateHtml(run: { action: "plan" | "drift" | "report"; status: string; drift_detected?: boolean }): string {
  return renderToStaticMarkup(<RunStateEvidence run={run} />);
}

describe("authoritative drift rendering", () => {
  it("renders detected, clean, and legacy-unknown drift results honestly", () => {
    expect(stateHtml({ action: "drift", status: "succeeded", drift_detected: true })).toContain("DRIFT");

    const clean = stateHtml({ action: "drift", status: "succeeded", drift_detected: false });
    expect(clean).toContain("COMPLETE");
    expect(clean).toContain("DRIFT RESULT · CLEAN");

    const unknown = stateHtml({ action: "drift", status: "succeeded" });
    expect(unknown).toContain("COMPLETE");
    expect(unknown).toContain("DRIFT RESULT UNKNOWN");
  });

  it("lets terminal failure dominate a detected drift result", () => {
    const html = stateHtml({ action: "drift", status: "failed", drift_detected: true });
    expect(html).toContain("FAILED");
    expect(html).not.toContain("Run state: DRIFT");
  });
});

describe("bounded cursor pagination", () => {
  it("round-trips opaque cursors through a bounded previous-page history", () => {
    const second = advanceCursor({}, "opaque:first");
    const third = advanceCursor(second, "opaque:second");
    expect(third).toEqual({ cursor: "opaque:second", history: ["", "opaque:first"] });
    expect(retreatCursor(third)).toEqual({ cursor: "opaque:first", history: [""] });
    expect(retreatCursor(retreatCursor(third))).toEqual({ cursor: undefined, history: undefined });
  });

  it("renders truthful next and previous controls", () => {
    const html = renderToStaticMarkup(
      <CursorPagination
        page={2}
        hasNext
        hasPrevious
        label="Account reference"
        onNext={() => undefined}
        onPrevious={() => undefined}
      />,
    );
    expect(html).toContain("PREVIOUS BOUNDED PAGE");
    expect(html).toContain("BOUNDED PAGE 2");
    expect(html).toContain("NEXT BOUNDED PAGE");
    expect(html).toContain("Account reference bounded pagination");
  });
});
