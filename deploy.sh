poetry export -f requirements.txt --without-hashes -o requirements.txt
zip pack *.py requirements.txt
yc serverless function version create \
  --function-name tg-bot \
  --runtime python39-preview \
  --entrypoint main.ss_entry \
  --memory 128m \
  --execution-timeout 3s \
  --source-path pack.zip \
  --service-account-id $SERVICE_ACC \
  --environment BOT_TOKEN=$BOT_TOKEN \
  --environment FOLDER=$FOLDER \
  --environment TG_USERS_WHITELIST=$TG_USERS_WHITELIST
