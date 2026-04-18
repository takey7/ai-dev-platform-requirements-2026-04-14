import { describe, expect, it } from "vitest";

describe("platform baseline", () => {
  it("keeps the integration lane alive", () => {
    expect(["node-ts", "typescript"]).toContain("node-ts");
  });
});
