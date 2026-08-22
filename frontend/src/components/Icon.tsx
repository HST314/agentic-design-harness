type IconName =
  | "archive"
  | "board"
  | "chevron-left"
  | "close"
  | "history"
  | "file"
  | "file-check"
  | "inbox"
  | "menu"
  | "message"
  | "more"
  | "panel-right"
  | "plan"
  | "pin"
  | "plus"
  | "search"
  | "rename"
  | "retry"
  | "settings"
  | "status"
  | "trash"
  | "upload";

const iconPaths: Record<IconName, React.JSX.Element> = {
  archive: <><path d="M3 6h18M5 6v14h14V6M9 10h6" /><path d="M4 3h16v3H4z" /></>,
  board: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16M15 4v16" /></>,
  "chevron-left": <path d="m15 18-6-6 6-6" />,
  close: <path d="M18 6 6 18M6 6l12 12" />,
  history: <><path d="M3 12a9 9 0 1 0 3-6.7L3 8" /><path d="M3 3v5h5M12 7v5l3 2" /></>,
  file: <><path d="M6 2h8l4 4v16H6z" /><path d="M14 2v5h5" /></>,
  "file-check": <><path d="M6 2h8l4 4v16H6z" /><path d="M14 2v5h5M9 14l2 2 4-4" /></>,
  inbox: <path d="M4 4h16l2 11h-6l-2 3h-4l-2-3H2L4 4Z" />,
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
  message: <path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4Z" />,
  more: <><circle cx="5" cy="12" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="19" cy="12" r="1" /></>,
  "panel-right": <><rect x="3" y="3" width="18" height="18" rx="2" /><path d="M15 3v18" /></>,
  plan: <><path d="M9 6h11M9 12h11M9 18h11" /><circle cx="4" cy="6" r="1" /><circle cx="4" cy="12" r="1" /><circle cx="4" cy="18" r="1" /></>,
  pin: <><path d="M12 17v5M7 3h10l-2 6 3 3H6l3-3z" /></>,
  plus: <path d="M12 5v14M5 12h14" />,
  search: <><circle cx="11" cy="11" r="7" /><path d="m20 20-4-4" /></>,
  rename: <><path d="m4 20 4.5-1 10-10-3.5-3.5-10 10zM13.5 7l3.5 3.5" /></>,
  retry: <><path d="M20 7v5h-5" /><path d="M19 12a7 7 0 1 0-2 5" /></>,
  settings: <><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.03 1.56V21h-4v-.08A1.7 1.7 0 0 0 8.95 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.58 15 1.7 1.7 0 0 0 3 14v-4a1.7 1.7 0 0 0 1.6-1.05 1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 9 4.58 1.7 1.7 0 0 0 10 3h4a1.7 1.7 0 0 0 1.03 1.6 1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 9 1.7 1.7 0 0 0 21 10v4a1.7 1.7 0 0 0-1.6 1Z" /></>,
  status: <><circle cx="12" cy="12" r="9" /><path d="M12 8v4l3 2" /></>,
  trash: <><path d="M4 7h16M9 7V4h6v3M7 7l1 14h8l1-14M10 11v6M14 11v6" /></>,
  upload: <><path d="M12 16V4M7 9l5-5 5 5" /><path d="M5 14v6h14v-6" /></>,
};

export function Icon({ name }: { name: IconName }): React.JSX.Element {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {iconPaths[name]}
    </svg>
  );
}
