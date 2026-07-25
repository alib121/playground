import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  getContentType,
} from "@whiskeysockets/baileys";
import express from "express";
import QRCode from "qrcode";
import Database from "better-sqlite3";
import pino from "pino";
import path from "path";
import { fileURLToPath } from "url";

const DATA_DIR = process.env.DATA_DIR || "/data";
const PORT = process.env.PORT || 3001;

// ── Database ──────────────────────────────────────────────────────────────────

const db = new Database(path.join(DATA_DIR, "messages.db"));
db.exec(`
  CREATE TABLE IF NOT EXISTS messages (
    id          TEXT    PRIMARY KEY,
    chat_id     TEXT    NOT NULL,
    chat_name   TEXT,
    sender_name TEXT,
    text        TEXT    NOT NULL,
    timestamp   INTEGER NOT NULL,
    is_group    INTEGER DEFAULT 0
  );
  CREATE TABLE IF NOT EXISTS chats (
    id   TEXT PRIMARY KEY,
    name TEXT
  );
  CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages (chat_id, timestamp);
`);

const stmtSaveChat = db.prepare("INSERT OR REPLACE INTO chats (id, name) VALUES (?, ?)");
const stmtSaveMsg = db.prepare(`
  INSERT OR IGNORE INTO messages (id, chat_id, chat_name, sender_name, text, timestamp, is_group)
  VALUES (?, ?, ?, ?, ?, ?, ?)
`);

// ── WhatsApp socket ───────────────────────────────────────────────────────────

let currentQR = null;
let connectionState = "starting";
const groupNames = {};

async function startSocket() {
  const { state, saveCreds } = await useMultiFileAuthState(
    path.join(DATA_DIR, "auth")
  );
  const { version } = await fetchLatestBaileysVersion();
  const logger = pino({ level: "silent" });

  const sock = makeWASocket({ version, auth: state, logger, printQRInTerminal: true });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      currentQR = qr;
      connectionState = "qr_pending";
      console.log("QR ready — visit /qr in a browser to scan");
    }
    if (connection === "open") {
      connectionState = "connected";
      currentQR = null;
      console.log("WhatsApp connected ✓");
    }
    if (connection === "close") {
      const code = lastDisconnect?.error?.output?.statusCode;
      if (code === DisconnectReason.loggedOut) {
        connectionState = "logged_out";
        console.log("Logged out. Delete /data/auth and restart to re-link.");
      } else {
        connectionState = "reconnecting";
        console.log("Disconnected — reconnecting in 5s…");
        setTimeout(startSocket, 5000);
      }
    }
  });

  sock.ev.on("groups.update", (updates) => {
    for (const u of updates) {
      if (u.id && u.subject) {
        groupNames[u.id] = u.subject;
        stmtSaveChat.run(u.id, u.subject);
      }
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    for (const msg of messages) {
      if (!msg.message || msg.key.fromMe) continue;

      const contentType = getContentType(msg.message);
      const text =
        msg.message?.conversation ||
        msg.message?.extendedTextMessage?.text ||
        msg.message?.[contentType]?.caption ||
        "";
      if (!text.trim()) continue;

      const chatId = msg.key.remoteJid;
      const isGroup = chatId?.endsWith("@g.us") ?? false;
      let chatName = groupNames[chatId] || chatId;

      if (isGroup && !groupNames[chatId]) {
        try {
          const meta = await sock.groupMetadata(chatId);
          chatName = meta.subject;
          groupNames[chatId] = chatName;
          stmtSaveChat.run(chatId, chatName);
        } catch {}
      }

      try {
        stmtSaveMsg.run(
          msg.key.id,
          chatId,
          chatName,
          msg.pushName || "unknown",
          text.trim(),
          Number(msg.messageTimestamp),
          isGroup ? 1 : 0
        );
      } catch {}
    }
  });
}

// ── HTTP API ──────────────────────────────────────────────────────────────────

const app = express();

app.get("/status", (_req, res) => {
  res.json({ status: connectionState });
});

// Visit this URL in a browser and scan with WhatsApp
app.get("/qr", async (_req, res) => {
  if (!currentQR) {
    return res.status(404).json({
      error:
        connectionState === "connected"
          ? "Already connected"
          : "No QR available yet — check back in a few seconds",
    });
  }
  const png = await QRCode.toBuffer(currentQR);
  res.set("Content-Type", "image/png");
  res.send(png);
});

// List group chats seen so far
app.get("/chats", (_req, res) => {
  const rows = db
    .prepare(
      `SELECT chat_id, chat_name, MAX(timestamp) as last_ts
       FROM messages WHERE is_group = 1
       GROUP BY chat_id ORDER BY last_ts DESC`
    )
    .all();
  res.json(rows);
});

// GET /messages?chat_name=Swanny&since=2026-07-11T00:00:00Z&limit=200
app.get("/messages", (req, res) => {
  const { chat_name, since, limit = 200 } = req.query;
  const sinceTs = since
    ? Math.floor(new Date(since).getTime() / 1000)
    : Math.floor(Date.now() / 1000) - 14 * 86400;

  const rows = chat_name
    ? db
        .prepare(
          `SELECT sender_name, text, timestamp, chat_name
           FROM messages
           WHERE lower(chat_name) LIKE lower(?) AND timestamp >= ?
           ORDER BY timestamp ASC LIMIT ?`
        )
        .all(`%${chat_name}%`, sinceTs, Number(limit))
    : db
        .prepare(
          `SELECT sender_name, text, timestamp, chat_name
           FROM messages
           WHERE timestamp >= ? AND is_group = 1
           ORDER BY timestamp ASC LIMIT ?`
        )
        .all(sinceTs, Number(limit));

  res.json(
    rows.map((r) => ({
      ...r,
      time: new Date(r.timestamp * 1000).toISOString().slice(0, 16).replace("T", " "),
    }))
  );
});

startSocket();
app.listen(PORT, () => console.log(`WhatsApp service listening on port ${PORT}`));
