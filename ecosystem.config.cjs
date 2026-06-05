// PM2 daemon config for the Dogar Trading Corporation Portal.
// Built by HussnainTechVertex Pvt Ltd.
//
// Usage:
//   pm2 start ecosystem.config.cjs
//   pm2 logs dogar-portal --nostream
//   pm2 restart dogar-portal --update-env
//   pm2 delete dogar-portal
module.exports = {
    apps: [{
        name: 'dogar-portal',
        script: 'uvicorn',
        args: 'app.main:app --host 0.0.0.0 --port 3000 --workers 1',
        cwd: '.',
        interpreter: 'none',
        env: {
            PYTHONUNBUFFERED: '1',
            PYTHONPATH: '.',
            ENV: 'development'
        },
        watch: false,
        instances: 1,
        exec_mode: 'fork',
        autorestart: true,
        max_restarts: 10,
        max_memory_restart: '512M'
    }]
};
