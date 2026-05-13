export default async function handler(req, res) {
  const { url } = req.query;
  if (!url) return res.status(400).json({ error: 'Parameter url diperlukan' });

  try {
    const response = await fetch(url);
    if (!response.ok) return res.status(502).json({ error: 'Gagal mengambil data detail' });
    const text = await response.text();
    res.setHeader('Content-Type', 'text/xml');
    res.status(200).send(text);
  } catch (error) {
    res.status(500).json({ error: 'Kesalahan proxy detail' });
  }
}