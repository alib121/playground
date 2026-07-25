import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  getContentType,
} from "@whiskeysockets/baileys";
import express from "express";
import QRCode from "qrcode";
import pino from "pino";
import path from "path";
import fs from "fs";

process.on("uncaughtException", (err) => { console.error("FATAL:", err.stack); process.exit(1); });
process.on("unhandledRejection", (reason) => { console.error("UNHANDLED:", reason); process.exit(1); });
console.log("WhatsApp service starting...");

const DATA_DIR = process.env.DATA_DIR || "/data";
const PORT = process.env.PORT || 3001;

fs.mkdirSync(DATA_DIR, { recursive: true });

// ── Storage (JSON file, no native deps) ──────────────────────────────────────

const MESSAGES_FILE = path.join(DATA_DIR, "messages.json");
let messages = [];

if (fs.existsSync(MESSAGES_FILE)) {
  try {
    messages = JSON.parse(fs.readFileSync(MESSAGES_FILE, "utf8"));
  } catch {
    messages = [];
  }
}

function saveMessage(msg) {
  messages.push(msg);
  if (messages.length > 10000) messages = messages.slice(-10000);
  fs.writeFileSync(MESSAGES_FILE, JSON.stringify(messages));
}

function queryMessages({ chatName, sinceTs, limit }) {
  return messages
    .filter((m) => m.is_group && m.timestamp >= sinceTs)
    .filter(
      (m) =>
        !chatName ||
        m.chat_name.toLowerCase().includes(chatName.toLowerCase())
    )
    .sort((a, b) => a.timestamp - b.timestamp)
    .slice(-limit)
    .map((m) => ({
      ...m,
      time: new Date(m.timestamp * 1000)
        .toISOString()
        .slice(0, 16)
        .replace("T", " "),
    }));
}

function listChats() {
  const latest = {};
  for (const m of messages) {
    if (!m.is_group) continue;
    if (!latest[m.chat_id] || m.timestamp > latest[m.chat_id].timestamp) {
      latest[m.chat_id] = m;
    }
  }
  return Object.values(latest)
    .sort((a, b) => b.timestamp - a.timestamp)
    .map((m) => ({ chat_id: m.chat_id, chat_name: m.chat_name, last_ts: m.timestamp }));
}

// ── WhatsApp socket ───────────────────────────────────────────────────────────

let currentQR = null;
let connectionState = "starting";
const groupNames = {};
const seenIds = new Set(messages.map((m) => m.id));

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
      if (u.id && u.subject) groupNames[u.id] = u.subject;
    }
  });

  sock.ev.on("messages.upsert", async ({ messages: msgs, type }) => {
    if (type !== "notify") return;
    for (const msg of msgs) {
      if (!msg.message || msg.key.fromMe) continue;
      if (seenIds.has(msg.key.id)) continue;

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
        } catch {}
      }

      const record = {
        id: msg.key.id,
        chat_id: chatId,
        chat_name: chatName,
        sender_name: msg.pushName || "unknown",
        text: text.trim(),
        timestamp: Number(msg.messageTimestamp),
        is_group: isGroup ? 1 : 0,
      };
      seenIds.add(record.id);
      saveMessage(record);
    }
  });
}

// ── HTTP API ──────────────────────────────────────────────────────────────────

const app = express();

app.get("/status", (_req, res) => {
  res.json({ status: connectionState });
});

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

app.get("/chats", (_req, res) => {
  res.json(listChats());
});

app.get("/messages", (req, res) => {
  const { chat_name, since, limit = 200 } = req.query;
  const sinceTs = since
    ? Math.floor(new Date(since).getTime() / 1000)
    : Math.floor(Date.now() / 1000) - 14 * 86400;
  res.json(queryMessages({ chatName: chat_name, sinceTs, limit: Number(limit) }));
});

startSocket();
app.listen(PORT, () => console.log(`WhatsApp service listening on port ${PORT}`));
