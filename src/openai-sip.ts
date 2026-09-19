import WebSocket from "ws";
import { verifyCallContext, type CallContext } from "./phone.js";

function required(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`Missing required environment variable: ${name}`);
  return value;
}

function findHeader(headers: Array<{ name: string; value: string }>, target: string): string | undefined {
  return headers.find((h) => h.name.toLowerCase() === target.toLowerCase())?.value;
}

async function openaiCallAction(callId: string, action: "accept" | "reject", body: unknown) {
  const response = await fetch(
    `https://api.openai.com/v1/realtime/calls/${encodeURIComponent(callId)}/${action}`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${required("OPENAI_API_KEY")}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    },
  );
  if (!response.ok) throw new Error(`OpenAI ${action} failed: ${response.status} ${await response.text()}`);
}

function instructionsFor(ctx: CallContext): string {
  return [
    "あなたは電話をかけているAIアシスタントです。必ず日本語で、自然かつ簡潔に話してください。",
    "通話開始時に自分がAIアシスタントであることを明確に名乗ってください。",
    "人間になりすましたり、権限・身分・事実を捏造してはいけません。",
    "相手が通話終了を求めたら、短く挨拶して終了してください。",
    "目的に必要な確認だけを行い、不必要な個人情報を聞かないでください。",
    "料金・契約・購入など新たな金銭的義務を勝手に確定しないでください。",
    `今回の通話目的: ${ctx.objective}`,
    ctx.instructions ? `追加指示: ${ctx.instructions}` : "",
  ].filter(Boolean).join("\n");
}

async function sendInitialGreeting(callId: string, objective: string) {
  await new Promise<void>((resolve, reject) => {
    const ws = new WebSocket(
      `wss://api.openai.com/v1/realtime?call_id=${encodeURIComponent(callId)}`,
      { headers: { Authorization: `Bearer ${required("OPENAI_API_KEY")}` } },
    );

    const timer = setTimeout(() => {
      ws.close();
      reject(new Error("Timed out opening OpenAI sideband WebSocket"));
    }, 8_000);

    ws.once("open", () => {
      clearTimeout(timer);
      ws.send(JSON.stringify({
        type: "response.create",
        response: {
          output_modalities: ["audio"],
          instructions:
            `まず「AIアシスタントです」と名乗り、電話の目的を一文で説明して会話を始めてください。目的: ${objective}`,
        },
      }));
      setTimeout(() => {
        ws.close();
        resolve();
      }, 1_000);
    });
    ws.once("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
  });
}

export async function handleRealtimeIncoming(event: any) {
  if (event?.type !== "realtime.call.incoming") return { handled: false };

  const callId = event?.data?.call_id as string | undefined;
  const headers = (event?.data?.sip_headers ?? []) as Array<{ name: string; value: string }>;
  if (!callId) throw new Error("Missing OpenAI call_id");

  const token = findHeader(headers, "X-Call-Context");
  if (!token) {
    await openaiCallAction(callId, "reject", { status_code: 403 });
    return { handled: true, accepted: false, reason: "missing_context" };
  }

  let ctx: CallContext;
  try {
    ctx = verifyCallContext(token);
  } catch {
    await openaiCallAction(callId, "reject", { status_code: 403 });
    return { handled: true, accepted: false, reason: "invalid_context" };
  }

  await openaiCallAction(callId, "accept", {
    type: "realtime",
    model: process.env.OPENAI_REALTIME_MODEL || "gpt-realtime-2.1",
    output_modalities: ["audio"],
    instructions: instructionsFor(ctx),
    audio: {
      input: {
        turn_detection: { type: "semantic_vad" },
      },
      output: {
        voice: "marin",
      },
    },
  });

  // Best effort. VAD will continue the conversation even if this greeting trigger fails.
  void sendInitialGreeting(callId, ctx.objective).catch((err) => {
    console.error("Initial greeting failed", err);
  });

  return { handled: true, accepted: true, callId };
}
