import { describe, expect, it } from "vitest";

import { describePlatform } from "../../src/index.js";

describe("describePlatform", () => {
  it("returns a friendly message", () => {
    expect(describePlatform("{{SERVICE_NAME}}")).toContain("{{SERVICE_NAME}}");
  });
});
