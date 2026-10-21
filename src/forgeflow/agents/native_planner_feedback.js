// v2 changes only validated error feedback; tool inputs and behavior stay shared.
import { createPlannerTool } from "./native_planner.js";

export default function nativePlannerFeedback(pi) {
  pi.registerTool(createPlannerTool(pi, undefined, "structured-v1"));
}
