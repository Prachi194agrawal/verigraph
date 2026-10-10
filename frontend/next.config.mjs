/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone", // small Docker image: node .next/standalone/server.js
  reactStrictMode: true,
};

export default nextConfig;
