/** Display exact decimal strings without routing money through binary floats. */
export function costMoney(value: string | null | undefined, lang: string, unknown: string): string {
  const match = typeof value === "string" ? /^(-?)(\d{1,24})(?:\.(\d{1,24}))?$/.exec(value) : null;
  if (!match) return unknown;
  const [, negative, whole, fraction = ""] = match;
  const cents = BigInt(whole) * 100n + BigInt((fraction + "00").slice(0, 2))
    + (fraction.length > 2 && fraction[2] >= "5" ? 1n : 0n);
  const units = cents / 100n;
  const signed = negative && cents > 0n ? (units === 0n ? -0 : -units) : units;
  const formatter = new Intl.NumberFormat(lang === "zh" ? "zh-CN" : "en-US", {
    style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
  return formatter.formatToParts(signed).map(part => part.type === "fraction"
    ? (cents % 100n).toString().padStart(2, "0") : part.value).join("");
}
