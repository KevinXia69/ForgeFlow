import { useEffect, useLayoutEffect, useMemo, useRef, type HTMLAttributes } from "react";
import { createEditor, $createParagraphNode, $createTextNode, $getRoot, type LexicalEditor } from "lexical";
import { registerPlainText } from "@lexical/plain-text";
import { createEmptyHistoryState, registerHistory } from "@lexical/history";
import { mergeRegister } from "@lexical/utils";

/** DSH's resident contenteditable host, reduced to the editor binding. */
function ComposerContentEditable({
  editor, editable, ...rest
}: HTMLAttributes<HTMLDivElement> & { editor: LexicalEditor | null; editable: boolean }) {
  const ref = useRef<HTMLDivElement | null>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!editor || !el) return;
    editor.setRootElement(el);
    return () => editor.setRootElement(null);
  }, [editor]);
  useLayoutEffect(() => {
    if (editor) editor.setEditable(editable);
  }, [editor, editable]);
  return (
    <div
      ref={ref}
      contentEditable={Boolean(editor) && editable}
      suppressContentEditableWarning
      role="textbox"
      aria-multiline="true"
      data-composer-input
      {...rest}
    />
  );
}

/**
 * ForgeFlow adapter over the DSH/Lexical composer surface. The editor shell is
 * DSH's; the surrounding InputBar machine (attachments, commands, queue) is
 * intentionally not claimed here yet.
 */
export function DshComposer({
  value, placeholder, ariaLabel, disabled, onChange, onSend,
}: {
  value: string;
  placeholder: string;
  ariaLabel: string;
  disabled: boolean;
  onChange: (value: string) => void;
  onSend: () => void;
}) {
  const onChangeRef = useRef(onChange);
  const onSendRef = useRef(onSend);
  onChangeRef.current = onChange;
  onSendRef.current = onSend;
  const editor = useMemo(() => createEditor({
    namespace: "forgeflow-composer",
    onError: (error) => { throw error; },
  }), []);

  useEffect(() => {
    const unregister = mergeRegister(
      registerPlainText(editor),
      editor.registerUpdateListener(({ editorState }) => {
        editorState.read(() => onChangeRef.current($getRoot().getTextContent()));
      }),
      registerHistory(editor, createEmptyHistoryState(), 1000),
    );
    return () => {
      unregister();
      editor.setRootElement(null);
    };
  }, [editor]);

  useEffect(() => {
    editor.update(() => {
      const root = $getRoot();
      if (root.getTextContent() === value) return;
      root.clear();
      const paragraph = $createParagraphNode();
      if (value) paragraph.append($createTextNode(value));
      root.append(paragraph);
    }, { discrete: true });
  }, [editor, value]);

  return (
    <ComposerContentEditable
      editor={editor}
      editable={!disabled}
      className="composer-input"
      data-placeholder={placeholder}
      data-empty={value.trim() === "" ? "true" : undefined}
      aria-label={ariaLabel}
      onKeyDown={(event) => {
        if (event.key !== "Enter" || event.shiftKey) return;
        // Enter confirms an IME candidate before it becomes a send command.
        // Safari can clear isComposing on that keydown, but keeps keyCode 229.
        if (disabled || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229 || editor.isComposing()) return;
        event.preventDefault();
        onSendRef.current();
      }}
    />
  );
}
