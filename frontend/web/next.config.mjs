/** @type {import('next').NextConfig} */
const apiBase = process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8765";

const nextConfig = {
  devIndicators: false,
  // Auto-memoise components & hooks at compile time. This drastically cuts
  // re-render cost during fast streaming, freeing the main thread for input
  // events (smooth scrolling).
  reactCompiler: true,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${apiBase}/:path*` },
    ];
  },
};

export default nextConfig;
