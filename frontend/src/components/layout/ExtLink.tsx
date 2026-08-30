import type { AnchorHTMLAttributes, ReactNode } from "react";

/**
 * Plain hyperlink to another site. Navigation only: nothing is loaded from the target,
 * and `Referrer-Policy: no-referrer` (nginx/Caddy) keeps the user's URL private.
 */
export function ExtLink({
  href,
  children,
  className,
  ...rest
}: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string; children: ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className={
        className ??
        "text-brand-600 underline decoration-slate-300 underline-offset-2 hover:text-brand-800 hover:decoration-brand-600"
      }
      {...rest}
    >
      {children}
    </a>
  );
}
