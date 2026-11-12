export type OrderLike = { ref: string; order_count?: number };

export function orderCount(order: OrderLike | string | null | undefined): number | null {
  if (!order) return null;
  if (typeof order === "string") return orderCountFromRef(order);
  if (Number.isInteger(order.order_count) && (order.order_count ?? 0) > 0) {
    return order.order_count ?? null;
  }
  return orderCountFromRef(order.ref);
}

function orderCountFromRef(ref: string): number | null {
  if (!ref) return null;
  const parts = ref.split(",").map(part => part.trim()).filter(Boolean);
  if (parts.length > 1) return parts.length;
  const range = /(\d+)\s*\.\.\s*(\d+)/.exec(ref);
  if (range) {
    const start = Number(range[1]);
    const end = Number(range[2]);
    if (Number.isFinite(start) && Number.isFinite(end) && end >= start) {
      return end - start + 1;
    }
  }
  return parts.length === 1 ? 1 : null;
}
