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
      <path
        d="M16 3.5v25M3.5 16h25M7.2 7.2l17.6 17.6M24.8 7.2 7.2 24.8"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
      />
      <circle cx="16" cy="16" r="4.25" fill="currentColor" />
      <circle cx="16" cy="16" r="1.75" fill="var(--mark-center, #173d35)" />
    </svg>
  );
}
