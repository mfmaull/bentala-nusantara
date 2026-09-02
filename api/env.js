export default async function handler(req, res) {
  // Baca API key dari Vercel Environment Variables
  // Set di Vercel Dashboard: Settings → Environment Variables
  const OPENWEATHER_API_KEY = process.env.OPENWEATHER_API_KEY || '';
  const MAP_SERVICE_KEY = process.env.MAP_SERVICE_KEY || '';

  res.setHeader('Content-Type', 'application/json');
  res.setHeader('Cache-Control', 'no-cache, no-store, must-revalidate');
  res.status(200).json({
    OPENWEATHER_API_KEY,
    MAP_SERVICE_KEY
  });
}