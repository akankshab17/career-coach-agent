import "server-only";
import { createHash, timingSafeEqual } from "node:crypto";
import { jwtVerify, SignJWT } from "jose";

// Login state lives in one signed, httpOnly cookie: a JWT (HS256, signed with SESSION_SECRET) holding a random session
// id and an expiry. Nothing is stored server-side, which suits Vercel's stateless functions. The session id gives the
// rate limiter (lib/ratelimit.ts) something to count per visitor.

export const SESSION_COOKIE = "ib_session";
export const SESSION_TTL_SECONDS = 7 * 24 * 60 * 60;
export const MIN_SECRET_LENGTH = 32;

export type Session = { sid: string; exp: number };

/** A missing or weak secret/code: the server is misconfigured, so login fails closed (500), never open. */
export class ConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ConfigError";
  }
}

function secretKey(): Uint8Array {
  const secret = process.env.SESSION_SECRET ?? "";
  if (secret.length < MIN_SECRET_LENGTH) {
    throw new ConfigError(`SESSION_SECRET must be set to at least ${MIN_SECRET_LENGTH} characters (openssl rand -hex 32).`);
  }
  return new TextEncoder().encode(secret);
}

/** Constant-time comparison with APP_ACCESS_CODE (both sides are hashed first, so lengths always match). */
export function accessCodeMatches(input: string): boolean {
  const expected = process.env.APP_ACCESS_CODE ?? "";
  if (!expected) throw new ConfigError("APP_ACCESS_CODE is not set.");
  const digest = (s: string) => createHash("sha256").update(s, "utf8").digest();
  return timingSafeEqual(digest(input), digest(expected));
}

export async function createSessionToken(now: Date = new Date()): Promise<string> {
  const iat = Math.floor(now.getTime() / 1000);
  return new SignJWT({ sid: crypto.randomUUID() })
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt(iat)
    .setExpirationTime(iat + SESSION_TTL_SECONDS)
    .sign(secretKey());
}

/** The session in a token, or null if it is missing, tampered with, expired, or signed with another secret. */
export async function verifySessionToken(token: string | undefined, now: Date = new Date()): Promise<Session | null> {
  if (!token) return null;
  try {
    const { payload } = await jwtVerify(token, secretKey(), { algorithms: ["HS256"], currentDate: now });
    if (typeof payload.sid !== "string" || typeof payload.exp !== "number") return null;
    return { sid: payload.sid, exp: payload.exp };
  } catch (err) {
    if (err instanceof ConfigError) throw err;
    return null;
  }
}

export const sessionCookieOptions = {
  httpOnly: true, // page scripts can't read it, so an XSS bug can't steal it
  secure: process.env.NODE_ENV === "production", // HTTPS only in production (localhost is plain HTTP)
  sameSite: "lax" as const, // not sent on cross-site POSTs (CSRF)
  path: "/",
  maxAge: SESSION_TTL_SECONDS,
};
