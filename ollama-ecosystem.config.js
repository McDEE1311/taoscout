module.exports = {
  apps: [
    {
      name: "ollama-main",
      script: "/usr/local/bin/ollama",
      args: "serve",
      env: {
        CUDA_VISIBLE_DEVICES: "0",
        OLLAMA_HOST: "127.0.0.1:11434",
        OLLAMA_KEEP_ALIVE: "24h"
      }
    },
    {
      name: "ollama-helper",
      script: "/usr/local/bin/ollama",
      args: "serve",
      env: {
        CUDA_VISIBLE_DEVICES: "1",
        OLLAMA_HOST: "127.0.0.1:11435",
        OLLAMA_KEEP_ALIVE: "24h"
      }
    },
    {
      name: "ollama-worker",
      script: "/usr/local/bin/ollama",
      args: "serve",
      env: {
        CUDA_VISIBLE_DEVICES: "2",
        OLLAMA_HOST: "127.0.0.1:11436",
        OLLAMA_KEEP_ALIVE: "24h"
      }
    }
  ]
}
