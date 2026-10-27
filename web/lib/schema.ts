import { z } from "zod";

// Request bodies the browser sends to this server. Checked here before anything reaches FastAPI.

export const LoginSchema = z.object({
  code: z.string().trim().min(1, "Enter the access code.").max(200),
});
