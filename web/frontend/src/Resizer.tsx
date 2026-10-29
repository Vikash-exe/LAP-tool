import { useRef } from "react";

interface Props {
  onResize: (deltaX: number) => void;
}

/** A draggable vertical divider between two side-by-side panes. */
export default function Resizer({ onResize }: Props) {
  const lastX = useRef(0);

  function handleMouseDown(e: React.MouseEvent) {
    e.preventDefault();
    lastX.current = e.clientX;

    function handleMouseMove(moveEvent: MouseEvent) {
      const delta = moveEvent.clientX - lastX.current;
      lastX.current = moveEvent.clientX;
      onResize(delta);
    }

    function handleMouseUp() {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    }

    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);
  }

  return (
    <div
      onMouseDown={handleMouseDown}
      style={{
        width: 6,
        cursor: "col-resize",
        background: "linear-gradient(90deg, #d3e3f6, #e8f1fb)",
        borderLeft: "1px solid #b9d0ec",
        borderRight: "1px solid #b9d0ec",
        flexShrink: 0,
      }}
    />
  );
}
