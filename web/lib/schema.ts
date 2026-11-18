import { z } from "zod";

// Request bodies the browser sends to this server. Checked here before anything reaches FastAPI; the limits mirror
// FastAPI's BriefRequest (src/api.py), which still validates again.

export const LoginSchema = z.object({
  code: z.string().trim().min(1, "Enter the access code.").max(200),
});

export const LEVELS = ["intern", "new-grad", "experienced"] as const;
export const DEFAULT_LOCATION = "United States"; // src/location.py DEFAULT_LOCATION

const requiredText = (label: string, max: number) =>
  z.string({ error: `${label} is required.` }).trim().min(1, `${label} is required.`).max(max, `${label} is too long (max ${max} characters).`);

export const BriefSchema = z.object({
  company: requiredText("Company", 200),
  role: requiredText("Role", 200),
  level: z.enum(LEVELS, { error: "Level must be intern, new-grad or experienced." }).default("new-grad"),
  location: z
    .string()
    .trim()
    .max(100, "Location is too long (max 100 characters).")
    .optional()
    .transform((v) => v || DEFAULT_LOCATION), // blank means the default, as on the CLI
  jd: z
    .string()
    .max(20_000, "The job description is too long (max 20,000 characters).")
    .optional()
    .transform((v) => (v && v.trim() ? v : undefined)),
  rerun: z.boolean().default(false), // build again even if a brief with the same inputs is less than 7 days old
});

export type BriefInput = z.input<typeof BriefSchema>;
export type BriefRequest = z.output<typeof BriefSchema>;

/** The body FastAPI's POST /briefs expects. */
export function toFastapiBody(req: BriefRequest) {
  return { company: req.company, role: req.role, level: req.level, location: req.location, jd: req.jd ?? null, force: req.rerun };
}
