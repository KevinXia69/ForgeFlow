/** Owner-scoped image reads for the ForgeFlow DSH composition. */
import { lstat, realpath, stat } from "node:fs/promises";
import { isAbsolute, relative, sep } from "node:path";

export const name = "forgeflow-image-guard";
export const inject = ["tools", "fs", "systemPrompt", "workingDirectory", "attachments"];

async function authorizedImage(root, requested) {
  // The API supplies absolute paths. URI and relative paths are deliberately
  // unsupported so the model cannot select a different resolution context.
  if (typeof root !== "string" || !isAbsolute(root) ||
      typeof requested !== "string" || !isAbsolute(requested)) {
    throw new Error("image_access_denied");
  }
  try {
    if ((await lstat(root)).isSymbolicLink()) throw new Error("denied");
    const [ownerRoot, target] = await Promise.all([realpath(root), realpath(requested)]);
    const within = relative(ownerRoot, target);
    if (!within || within === ".." || within.startsWith(`..${sep}`) || isAbsolute(within) ||
        !(await stat(target)).isFile()) {
      throw new Error("denied");
    }
    return target;
  } catch {
    // Do not reveal whether another owner's path exists.
    throw new Error("image_access_denied");
  }
}

export async function apply(ctx, config) {
  if (typeof config?.root !== "string" || !isAbsolute(config.root) ||
      typeof config?.toolModule !== "string" || !config.toolModule.startsWith("file:")) {
    throw new Error("image_scope_required");
  }
  ctx.on("tools/execute", async (exec, next) => {
    if (exec.name === "read_image") {
      const path = await authorizedImage(config.root, exec.arguments?.file_path);
      // Pass exactly the checked canonical path to the native implementation.
      exec.arguments = { file_path: path };
    }
    return next();
  });
  // scoped_patch disables the unguarded file-tools plugin. Register only its
  // native image tool here, after installing the mandatory authorization gate.
  // Failure to load this module leaves every filesystem tool unavailable.
  const tools = await import(config.toolModule);
  tools.apply(ctx, tools.Config({ read: false, readImage: true, write: false, edit: false }));
}
