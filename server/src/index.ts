import { createApp } from './app.js';

const port = Number(process.env.PORT ?? 8787);
const { server } = createApp();

server.listen(port, () => {
  console.log(`AwLPay SettlementEngine listening on http://127.0.0.1:${port}`);
  console.log('Mock settlement only — no live chain. CoS gates Fly/npm.');
});
