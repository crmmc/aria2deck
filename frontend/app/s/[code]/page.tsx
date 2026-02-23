import SharePageClient from "./SharePageClient";
export function generateStaticParams() {
  return [{ code: "_" }];
}
export default function SharePage() {
  return <SharePageClient />;
}