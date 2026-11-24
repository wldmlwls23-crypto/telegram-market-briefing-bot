import { defineRailway, github, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const telegramMarketBriefingBot = github("wldmlwls23-crypto/telegram-market-briefing-bot", { branch: "main", checkSuites: false });

  const telegramMarketBriefingBotVolume = volume("telegram-market-briefing-bot-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "sfo", sizeMB: 500 });
  const telegramMarketBriefingBot2 = service("telegram-market-briefing-bot", {
    source: telegramMarketBriefingBot,
    build: { buildEnvironment: "V3", builder: "RAILPACK" },
    replicas: { "sfo": 1 },
    start: "python main.py",
    healthcheck: "/health",
    healthcheckTimeout: 30,
    deploy: {
      numReplicas: 1,
      sleepApplication: true,
      restartPolicyType: "ON_FAILURE",
      restartPolicyMaxRetries: 3,
      limitOverride: { containers: { cpu: 0.25, memoryBytes: 268435456 } },
    },
    volumeMounts: { "/data": telegramMarketBriefingBotVolume },
  });
  const marketPulseCron = service("market-pulse-cron", {
    source: telegramMarketBriefingBot,
    build: { buildEnvironment: "V3", builder: "RAILPACK" },
    start: "python -m jin_market_pulse.cron",
    replicas: { "sfo": 1 },
    deploy: {
      cronSchedule: "5,20,35,50 * * * *",
      sleepApplication: false,
      restartPolicyType: "NEVER",
      limitOverride: { containers: { cpu: 0.25, memoryBytes: 268435456 } },
    },
  });

  return project("compassionate-recreation", {
    variables: { managed: false },
    resources: [telegramMarketBriefingBot2, marketPulseCron, telegramMarketBriefingBotVolume],
  });
});
