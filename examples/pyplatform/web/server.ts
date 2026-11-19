import express from "express";

const app = express();
const CATALOG = "http://catalog.internal";

app.get("/health", (_req, res) => {
  res.json({ ok: true });
});

app.post("/proxy/orders", async (_req, res) => {
  const r = await fetch(`${CATALOG}/api/orders`, { method: "POST" });
  res.json(await r.json());
});

app.get("/rpc/tax", async (_req, res) => {
  const supabase = { rpc: (name: string) => name };
  supabase.rpc("apply_order_tax");
  res.json({ ok: true });
});
