import { redirect } from "next/navigation";
import type { Metadata } from "next";

export const metadata: Metadata = { title: "aria2 控制器" };

export default function Home() {
  redirect("/tasks");
}
