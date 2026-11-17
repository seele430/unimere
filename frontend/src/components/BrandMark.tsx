type BrandMarkProps = {
  size?: number;
  className?: string;
};

export default function BrandMark({ size = 32, className }: BrandMarkProps) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      aria-hidden="true"
    >
      <g stroke="currentColor" strokeLinecap="round">
        <path
          d="M7 9.5v6.5c0 6 3.5 9.5 9 9.5s9-3.5 9-9.5V9.5"
          strokeWidth="2.2"
        />
        <path
          d="M11 11.5v4.5c0 4 2 6 5 6s5-2 5-6v-4.5"
          strokeWidth="1.6"
          opacity=".72"
        />
      </g>
      <circle cx="16" cy="16" r="2.6" fill="var(--mark-center, #5db9b4)" />
    </svg>
  );
}
