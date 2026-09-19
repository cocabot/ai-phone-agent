import express from "express";
import OpenAI from "openai";
import { toNodeHandler } from "@modelcontextprotocol/node";
import { mcpHandler } from "./mcp.js";
import { handleRealtimeIncoming } from "./openai-sip.js";

const port = Number(process.env.PORT || 3000);
const app = express();
app.disable("x-powered-by");

function mcpAuth(req: express.Request, res: express.Response, next: express.NextFunction) {
  const expected = process.env.MCP_BEARER_TOKEN;
  if (!expected) {
    res.status(500).json({ error: "MCP_BEARER_TOKEN is not configured" });
    return;
  }
  const auth = req.header("authorization");
  if (auth !== `Bearer ${expected}`) {
    res.status(401).setHeader("WWW-Authenticate", "Bearer").json({ error: "Unauthorized" });
    return;
  }
  next();
}

const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY,
  webhookSecret: process.env.OPENAI_WEBHOOK_SECRET,
});

app.get("/health", (_req, res) => {
  res.json({ ok: true, service: "ai-phone-agent-jp" });
});

// Webhook signature verification requires the exact raw request body.
app.post("/webhooks/openai", express.text({ type: "application/json" }), async (req, res) => {
  try {
    const event = await openai.webhooks.unwrap(req.body, req.headers);
    const result = await handleRealtimeIncoming(event);
    res.status(200).json(result);
  } catch (error) {
    if (error instanceof OpenAI.InvalidWebhookSignatureError) {
      res.status(400).json({ error: "Invalid OpenAI webhook signature" });
      return;
    }
    console.error(error);
    res.status(500).json({ error: error instanceof Error ? error.message : "Internal error" });
  }
});

const nodeMcp = toNodeHandler(mcpHandler);
app.all("/mcp", mcpAuth, (req, res) => {
  void nodeMcp(req, res);
});

app.listen(port, "0.0.0.0", () => {
  console.log(`ai-phone-agent-jp listening on :${port}`);
});
