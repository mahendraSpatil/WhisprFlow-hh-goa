// Monaco is bundled, not fetched: the React wrapper loads it from a CDN by default, which would break
// the patch view offline and leak a request to a third party. Importing this module configures it once.
//
// Only the editor core and the Python grammar are imported (not the full "monaco-editor" entry,
// which bundles the TypeScript, CSS, HTML and JSON language services: megabytes a diff never uses).
import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor/editor/editor.api";
import "monaco-editor/features/codicon/register"; // the icon font for the fold and gutter glyphs
import "monaco-editor/languages/definitions/python/register";
import EditorWorker from "monaco-editor/editor/editor.worker?worker";

(self as unknown as { MonacoEnvironment: monaco.Environment }).MonacoEnvironment = {
  getWorker: () => new EditorWorker(),
};
loader.config({ monaco });

export const THEME = "codeloop-dark";

monaco.editor.defineTheme(THEME, {
  base: "vs-dark",
  inherit: true,
  rules: [
    { token: "comment", foreground: "5f6b85", fontStyle: "italic" },
    { token: "keyword", foreground: "c792ea" },
    { token: "string", foreground: "a5e075" },
    { token: "number", foreground: "f78c6c" },
    { token: "type", foreground: "82aaff" },
  ],
  colors: {
    "editor.background": "#0b0d12",
    "editor.foreground": "#d3d9e8",
    "editorLineNumber.foreground": "#565f73",
    "editorLineNumber.activeForeground": "#b4bccd",
    "editorGutter.background": "#0b0d12",
    "editor.lineHighlightBackground": "#12151c",
    "editorWidget.background": "#12151c",
    "scrollbarSlider.background": "#2e354666",
    "diffEditor.insertedLineBackground": "#3ddc9718",
    "diffEditor.insertedTextBackground": "#3ddc9733",
    "diffEditor.removedLineBackground": "#ff637018",
    "diffEditor.removedTextBackground": "#ff637033",
    "diffEditor.border": "#222734",
    "diffEditor.diagonalFill": "#222734",
  },
});
