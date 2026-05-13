export default async function handler(req, res) {
  const targetUrl = 'https://www.bmkg.go.id/alerts/nowcast/id/rss.xml';
  try {
    const response = await fetch(targetUrl);
    if (!response.ok) {
      return res.status(502).json({ error: 'Gagal mengambil data dari BMKG' });
    }
    const text = await response.text();
    // Kembalikan XML sebagai teks, frontend akan mem-parse-nya
    res.setHeader('Content-Type', 'text/xml');
    res.status(200).send(text);
  } catch (error) {
    res.status(500).json({ error: 'Kesalahan server proxy' });
  }
}