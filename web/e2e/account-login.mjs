/** Account login + MFA + narrow/keyboard/reconnect acceptance. */
import crypto from "node:crypto";
import { mkdir } from "node:fs/promises";
import { chromium } from "playwright-core";

const BASE = process.env.FORGEFLOW_URL;
const EMAIL = process.env.FORGEFLOW_EMAIL;
const PASSWORD = process.env.FORGEFLOW_PASSWORD;
const SECRET = process.env.FORGEFLOW_TOTP_SECRET;
const OUT = process.env.FORGEFLOW_SCREENSHOT_DIR || "/tmp/forgeflow-accounts";
if (!BASE || !EMAIL || !PASSWORD || !SECRET) throw new Error("missing account acceptance env");
await mkdir(OUT, { recursive: true });

const ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
function decodeBase32(value) {
  let bits = 0, buffer = 0;
  const out = [];
  for (const char of value.replace(/=+$/, "").toUpperCase()) {
    const index = ALPHABET.indexOf(char);
    if (index < 0) continue;
    buffer = (buffer << 5) | index; bits += 5;
    if (bits >= 8) { out.push((buffer >>> (bits - 8)) & 0xff); bits -= 8; }
  }
  return Buffer.from(out);
}
function totp(secret, offset = 0) {
  const counter = Math.floor(Date.now() / 30000) + offset;
  const buffer = Buffer.alloc(8); buffer.writeBigUInt64BE(BigInt(counter));
  const digest = crypto.createHmac("sha1", decodeBase32(secret)).update(buffer).digest();
  const position = digest[digest.length - 1] & 0x0f;
  const value = (digest.readUInt32BE(position) & 0x7fffffff) % 1000000;
  return String(value).padStart(6, "0");
}
const assert = (value, message) => { if (!value) throw new Error(message); };

const browser = await chromium.launch({ channel: "chrome", headless: true });
try {
  for (const lang of ["zh", "en"]) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    const errors = []; page.on("pageerror", error => errors.push(String(error)));
    await page.goto(BASE, { waitUntil: "domcontentloaded" });
    await page.locator("#language-select").click();
    await page.getByRole("option", { name: lang === "zh" ? "简体中文" : "English", exact: true }).click();
    await page.locator("#account-email").waitFor();
    assert(await page.locator("#invite-code").count() === 0, "invite form still offered with accounts enabled");
    // Keyboard path: email -> password -> submit.
    await page.locator("#account-email").focus();
    await page.keyboard.type(EMAIL);
    await page.keyboard.press("Tab");
    await page.keyboard.type(PASSWORD);
    await page.keyboard.press("Tab");
    await page.keyboard.press("Enter");
    await page.locator("#mfa-code").waitFor();
    await page.screenshot({ path: `${OUT}/${lang}-mfa.png`, animations: "disabled" });
    // A wrong code is reported without leaking whether the account exists.
    await page.locator("#mfa-code").fill("000000");
    await page.keyboard.press("Enter");
    await page.getByRole("alert").waitFor();
    const wrong = await page.getByRole("alert").innerText();
    assert(wrong.trim().length > 0, "wrong MFA code had no message");
    await page.locator("#mfa-code").fill(totp(SECRET));
    await page.keyboard.press("Enter");
    await page.locator("[data-home-view]").waitFor({ timeout: 20000 });
    assert(await page.locator("#account-email").count() === 0, "login form still visible after MFA");
    const me = await page.request.get(`${BASE}/api/accounts/me`);
    assert(me.status() === 200, `account session not active: ${me.status()}`);
    // Refresh and SSE reconnect must keep the session and not replay the login.
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.locator("[data-home-view]").waitFor({ timeout: 20000 });
    assert(await page.locator("#account-email").count() === 0, "refresh returned to login");
    await page.screenshot({ path: `${OUT}/${lang}-workbench.png`, animations: "disabled" });
    if (errors.length) throw new Error(errors.join("\n"));
    await context.close();
    console.log(`accounts=${lang}:ok`);
  }
  const narrow = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await narrow.newPage();
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await page.locator("#account-email").waitFor();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  assert(overflow <= 1, `narrow login overflows by ${overflow}px`);
  await page.screenshot({ path: `${OUT}/narrow-login.png`, animations: "disabled", fullPage: true });
  await narrow.close();
  console.log("accounts=narrow:ok");
} finally { await browser.close(); }
