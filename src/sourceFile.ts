import { message, open } from "@tauri-apps/plugin-dialog";

/**
 * True if a thrown error (from invoke) is the sidecar telling us the
 * supplied `input_path` doesn't exist — the signal to prompt the user
 * to locate the file and retry the call.
 */
export function isMissingInputError(err: unknown): boolean {
  const s = err instanceof Error ? err.message : String(err);
  return (
    s.includes("-32602") &&
    (s.includes("input_path") || s.includes("is not a readable file"))
  );
}

/**
 * Show a "file moved" notice, then prompt the user to locate the
 * remediated IMSCC via an Open dialog. Returns the selected path, or
 * null if cancelled.
 */
export async function promptForSourceIMSCC(
  cachedPath: string,
): Promise<string | null> {
  await message(
    `Remedy Canvas Desktop can't find the remediated IMSCC at:\n\n${cachedPath}\n\nPlease locate it so we can continue.`,
    { title: "Remediated IMSCC moved", kind: "warning" },
  );
  const picked = await open({
    title: "Locate the remediated IMSCC",
    multiple: false,
    directory: false,
    filters: [{ name: "IMSCC", extensions: ["imscc"] }],
    defaultPath: cachedPath,
  });
  return typeof picked === "string" ? picked : null;
}
