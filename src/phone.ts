import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";

function required(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`Missing required environment variable: ${name}`);
  return value;
}

export type CallContext = {
  objective: string;
  instructions?: string;
  issuedAt: number;
};

type StoredContext = CallContext & { expiresAt: number };

// MVP store: the SIP header only carries a short signed opaque ID because
// Twilio limits SIP URIs to 255 characters. For horizontal scaling, replace
// this Map with Redis/KV using the same token -> context interface.
const contexts = new Map<string, StoredContext>();

function cleanupContexts() {
  const now = Date.now();
  for (const [id, ctx] of contexts) {
    if (ctx.expiresAt <= now) contexts.delete(id);
  }
}

export function signCallContext(input: Omit<CallContext, "issuedAt">): string {
  cleanupContexts();
  const id = randomBytes(16).toString("base64url");
  const sig = createHmac("sha256", required("CALL_CONTEXT_SECRET")).update(id).digest("base64url");
  contexts.set(id, {
    ...input,
    issuedAt: Date.now(),
    expiresAt: Date.now() + 10 * 60_000,
  });
  return `${id}.${sig}`;
}

export function verifyCallContext(token: string): CallContext {
  cleanupContexts();
  const [id, providedSig] = token.split(".");
  if (!id || !providedSig) throw new Error("Malformed call context");

  const expectedSig = createHmac("sha256", required("CALL_CONTEXT_SECRET")).update(id).digest();
  const actualSig = Buffer.from(providedSig, "base64url");
  if (expectedSig.length !== actualSig.length || !timingSafeEqual(expectedSig, actualSig)) {
    throw new Error("Invalid call context signature");
  }

  const stored = contexts.get(id);
  if (!stored) throw new Error("Unknown or expired call context");
  contexts.delete(id);

  const { expiresAt: _expiresAt, ...ctx } = stored;
  return ctx;
}

export function normalizeJapanNumber(raw: string): string {
  let n = raw.replace(/[\s()-]/g, "");
  if (n.startsWith("0081")) n = "+" + n.slice(2);
  if (n.startsWith("81") && !n.startsWith("+")) n = "+" + n;
  if (n.startsWith("0")) n = "+81" + n.slice(1);

  if (!/^\+81\d{9,10}$/.test(n)) {
    throw new Error("Japan-first MVP only accepts Japanese E.164 numbers (+81...)");
  }

  const blocked = [
    "+81110",
    "+81118",
    "+81119",
  ];
  if (blocked.includes(n)) throw new Error("Emergency-service numbers are blocked");
  if (n.startsWith("+81570")) throw new Error("0570 service numbers are blocked in this MVP");
  if (n.startsWith("+81990")) throw new Error("0990 premium-rate numbers are blocked in this MVP");

  return n;
}

function xmlEscape(value: string): string {
  return value.replace(/[<>&'"]/g, (ch) => ({
    "<": "&lt;",
    ">": "&gt;",
    "&": "&amp;",
    "'": "&apos;",
    '"': "&quot;",
  }[ch]!));
}

function twilioAuth(): string {
  return "Basic " + Buffer.from(`${required("TWILIO_ACCOUNT_SID")}:${required("TWILIO_AUTH_TOKEN")}`).toString("base64");
}

async function twilioRequest(path: string, init: RequestInit = {}) {
  const sid = required("TWILIO_ACCOUNT_SID");
  const response = await fetch(`https://api.twilio.com/2010-04-01/Accounts/${sid}${path}`, {
    ...init,
    headers: {
      Authorization: twilioAuth(),
      ...(init.headers ?? {}),
    },
  });
  const text = await response.text();
  let data: any = text;
  try { data = JSON.parse(text); } catch {}
  if (!response.ok) {
    throw new Error(`Twilio error ${response.status}: ${typeof data === "string" ? data : JSON.stringify(data)}`);
  }
  return data;
}

export async function createPhoneCall(params: {
  to: string;
  objective: string;
  instructions?: string;
}) {
  const to = normalizeJapanNumber(params.to);
  const from = normalizeJapanNumber(required("TWILIO_FROM_NUMBER"));
  const projectId = required("OPENAI_PROJECT_ID");
  const context = signCallContext({ objective: params.objective, instructions: params.instructions });

  const sipUri =
    `sip:${projectId}@sip.api.openai.com;transport=tls;edge=tokyo?x-call-context=${encodeURIComponent(context)}`;

  const twiml = [
    "<Response>",
    '<Say language="ja-JP">AIアシスタントからのお電話です。これからAIが会話します。</Say>',
    `<Dial callerId="${xmlEscape(from)}" timeout="30" timeLimit="900">`,
    `<Sip>${xmlEscape(sipUri)}</Sip>`,
    "</Dial>",
    "</Response>",
  ].join("");

  const form = new URLSearchParams({
    To: to,
    From: from,
    Twiml: twiml,
    Timeout: "30",
    TimeLimit: "900",
  });

  const call = await twilioRequest("/Calls.json", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: form,
  });

  return {
    callSid: call.sid as string,
    status: call.status as string,
    to,
    from,
  };
}

export async function getPhoneCall(callSid: string) {
  if (!/^CA[0-9a-fA-F]{32}$/.test(callSid)) throw new Error("Invalid Twilio Call SID");
  const call = await twilioRequest(`/Calls/${callSid}.json`);
  return {
    callSid: call.sid,
    status: call.status,
    to: call.to,
    from: call.from,
    durationSeconds: call.duration ? Number(call.duration) : null,
    startTime: call.start_time ?? null,
    endTime: call.end_time ?? null,
    price: call.price ?? null,
    priceUnit: call.price_unit ?? null,
  };
}

export async function hangupPhoneCall(callSid: string) {
  if (!/^CA[0-9a-fA-F]{32}$/.test(callSid)) throw new Error("Invalid Twilio Call SID");
  const form = new URLSearchParams({ Status: "completed" });
  const call = await twilioRequest(`/Calls/${callSid}.json`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: form,
  });
  return { callSid: call.sid, status: call.status };
}
