import { useCallback, useState, DragEvent } from "react";
import { open } from "@tauri-apps/plugin-dialog";

type Props = {
  selectedPath: string | null;
  onSelect: (path: string) => void;
  disabled?: boolean;
};

const EXTENSIONS = ["imscc", "zip"];

function hasAcceptedExtension(name: string): boolean {
  const lower = name.toLowerCase();
  return EXTENSIONS.some((ext) => lower.endsWith(`.${ext}`));
}

function baseName(path: string): string {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

export function DropZone({ selectedPath, onSelect, disabled }: Props) {
  const [dragging, setDragging] = useState(false);

  const pickFile = useCallback(async () => {
    if (disabled) return;
    const result = await open({
      multiple: false,
      directory: false,
      title: "Select IMSCC course export",
      filters: [{ name: "IMSCC archive", extensions: EXTENSIONS }],
    });
    if (typeof result === "string") {
      onSelect(result);
    }
  }, [onSelect, disabled]);

  const onDragOver = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    if (!disabled) setDragging(true);
  };
  const onDragLeave = () => setDragging(false);

  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragging(false);
    if (disabled) return;
    // Tauri's drag-and-drop usually surfaces via the webview drop event with
    // File objects but no real path. If the browser exposes a path (Tauri 2
    // attaches it on the event.payload for file-drop events), prefer that;
    // otherwise fall back to the file picker.
    const dropped = e.dataTransfer.files?.[0];
    if (dropped && hasAcceptedExtension(dropped.name)) {
      // Browsers don't expose an absolute filesystem path on File objects.
      // Tauri surfaces drops via a dedicated event in lib.rs, but for a
      // progressive enhancement we fall back to the native picker so the
      // user still gets a usable path.
      void pickFile();
    } else {
      void pickFile();
    }
  };

  return (
    <div
      className={`dropzone ${dragging ? "dropzone--drag" : ""} ${
        selectedPath ? "dropzone--filled" : ""
      } ${disabled ? "dropzone--disabled" : ""}`}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      onClick={pickFile}
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-disabled={disabled || undefined}
      aria-label={
        selectedPath
          ? `IMSCC file selected: ${baseName(selectedPath)}. Press Enter to choose a different file.`
          : "Drop an IMSCC file here, or press Enter to browse."
      }
      onKeyDown={(e) => {
        if (disabled) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          void pickFile();
        }
      }}
    >
      {selectedPath ? (
        <>
          <div className="dropzone__filename">{baseName(selectedPath)}</div>
          <div className="dropzone__path" title={selectedPath}>
            {selectedPath}
          </div>
          <div className="dropzone__hint">Click to choose a different file</div>
        </>
      ) : (
        <>
          <div className="dropzone__title">Drop an IMSCC file here</div>
          <div className="dropzone__hint">or click to browse</div>
          <div className="dropzone__sub">.imscc or .zip exports from Canvas</div>
        </>
      )}
    </div>
  );
}
