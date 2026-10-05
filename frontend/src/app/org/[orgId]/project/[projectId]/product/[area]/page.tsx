"use client";
import { useParams } from "next/navigation";
import { ProductConsole } from "@/components/features/product/ProductConsole";
import { productAreas } from "@/components/features/product/ProductShell";
export default function ProductPage() {
  const { area } = useParams<{ area: string }>();
  if (!productAreas.some((value) => value === area))
    return <p>NOT_FOUND: 页面不存在</p>;
  return <ProductConsole key={area} area={area} />;
}
