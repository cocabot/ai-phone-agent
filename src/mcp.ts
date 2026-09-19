import { createMcpHandler, McpServer } from "@modelcontextprotocol/server";
import * as z from "zod/v4";
import { createPhoneCall, getPhoneCall, hangupPhoneCall } from "./phone.js";

function textResult(value: unknown, isError = false) {
  return {
    content: [{ type: "text" as const, text: JSON.stringify(value, null, 2) }],
    ...(isError ? { isError: true } : {}),
  };
}

export const mcpHandler = createMcpHandler(() => {
  const server = new McpServer({
    name: "ai-phone-agent-jp",
    version: "0.1.0",
  });

  server.registerTool(
    "make_phone_call",
    {
      description:
        "Place one user-approved AI voice call to a Japanese phone number. The recipient is informed that the caller is an AI. Only call when the user explicitly authorized this exact call.",
      inputSchema: z.object({
        to: z.string().describe("Japanese phone number, preferably E.164 +81 format"),
        objective: z.string().min(1).max(500).describe("Concrete purpose of the call"),
        instructions: z.string().max(1000).optional().describe("Optional constraints or facts the AI may use"),
        confirmed: z.literal(true).describe("Must be true only after explicit user authorization"),
      }),
    },
    async ({ to, objective, instructions }) => {
      try {
        const result = await createPhoneCall({ to, objective, instructions });
        return textResult(result);
      } catch (error) {
        return textResult({ error: error instanceof Error ? error.message : String(error) }, true);
      }
    },
  );

  server.registerTool(
    "get_phone_call_status",
    {
      description: "Get the current or final status of a phone call previously created by this plugin.",
      inputSchema: z.object({
        callSid: z.string().describe("Twilio Call SID returned by make_phone_call"),
      }),
    },
    async ({ callSid }) => {
      try {
        return textResult(await getPhoneCall(callSid));
      } catch (error) {
        return textResult({ error: error instanceof Error ? error.message : String(error) }, true);
      }
    },
  );

  server.registerTool(
    "hangup_phone_call",
    {
      description: "End a phone call previously created by this plugin. Use only when the user asks to stop/end the call.",
      inputSchema: z.object({
        callSid: z.string().describe("Twilio Call SID returned by make_phone_call"),
      }),
    },
    async ({ callSid }) => {
      try {
        return textResult(await hangupPhoneCall(callSid));
      } catch (error) {
        return textResult({ error: error instanceof Error ? error.message : String(error) }, true);
      }
    },
  );

  return server;
});
