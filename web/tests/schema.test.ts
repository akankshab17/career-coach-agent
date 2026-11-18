import { describe, expect, it } from "vitest";
import { BriefSchema, toFastapiBody } from "@/lib/schema";

const parse = (body: unknown) => BriefSchema.safeParse(body);
const firstError = (body: unknown) => {
  const r = parse(body);
  return r.success ? null : r.error.issues[0]?.message;
};

describe("BriefSchema", () => {
  it("fills defaults and trims", () => {
    const r = parse({ company: "  stripe ", role: " Software Engineer " });
    expect(r.success && r.data).toEqual({
      company: "stripe",
      role: "Software Engineer",
      level: "new-grad",
      location: "United States",
      jd: undefined,
      rerun: false,
    });
  });

  it("keeps a given level, location, JD and rerun", () => {
    const r = parse({ company: "Acme", role: "SWE", level: "intern", location: "Canada", jd: "Build things.", rerun: true });
    expect(r.success && toFastapiBody(r.data)).toEqual({
      company: "Acme", role: "SWE", level: "intern", location: "Canada", jd: "Build things.", force: true,
    });
  });

  it("turns a blank location into the default and a blank JD into none", () => {
    const r = parse({ company: "Acme", role: "SWE", location: "   ", jd: " \n " });
    expect(r.success && toFastapiBody(r.data)).toMatchObject({ location: "United States", jd: null });
  });

  it.each([
    [{ role: "SWE" }, "Company is required."],
    [{ company: "   ", role: "SWE" }, "Company is required."],
    [{ company: "Acme", role: "" }, "Role is required."],
    [{ company: "x".repeat(201), role: "SWE" }, "Company is too long (max 200 characters)."],
    [{ company: "Acme", role: "SWE", level: "senior" }, "Level must be intern, new-grad or experienced."],
    [{ company: "Acme", role: "SWE", location: "x".repeat(101) }, "Location is too long (max 100 characters)."],
    [{ company: "Acme", role: "SWE", jd: "x".repeat(20_001) }, "The job description is too long (max 20,000 characters)."],
    [{ company: "Acme", role: "SWE", rerun: "yes" }, expect.any(String)],
    [null, expect.any(String)],
  ])("rejects %j", (body, message) => {
    expect(firstError(body)).toEqual(message);
  });
});
