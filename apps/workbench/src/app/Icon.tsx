type IconName =
  | "grid"
  | "chat"
  | "activity"
  | "arrow"
  | "check"
  | "shield"
  | "store"
  | "bolt"
  | "chevron"
  | "plus"
  | "settings"
  | "box";

const paths: Record<IconName, string> = {
  grid: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  chat: "M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-3 2V11.5A8.5 8.5 0 0 1 9.5 3h3a8.5 8.5 0 0 1 8.5 8.5Z M7 9h9 M7 13h6",
  activity: "M3 12h4l3-8 4 16 3-8h4",
  arrow: "M5 12h14 M13 6l6 6-6 6",
  check: "m5 12 4 4L19 6",
  shield: "m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z m-4 9 3 3 5-6",
  store:
    "M3 10v11h18V10 M2 10l2-7h16l2 7 M2 10c2 3 4 3 6 0 2 3 4 3 6 0 2 3 5 3 8 0 M9 21v-7h6v7",
  bolt: "m13 2-9 12h7l-1 8 10-13h-8l1-7Z",
  chevron: "m9 5 7 7-7 7",
  plus: "M12 5v14 M5 12h14",
  settings: "M4 7h16 M4 17h16 M8 4v6 M16 14v6",
  box: "m12 2 9 5v10l-9 5-9-5V7l9-5Z m-9 5 9 5 9-5 M12 12v10 M7 4.5l9 5",
};

export function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name]} />
    </svg>
  );
}

export function Brand() {
  return (
    <div className="brand">
      <span className="brand-mark">
        <Icon name="box" size={24} />
      </span>
      <span>
        EcomAgent<small>COMMERCE OPERATIONS</small>
      </span>
    </div>
  );
}
