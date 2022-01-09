poetry export -f requirements.txt --without-hashes -o requirements.txt
zip pack main.py requirements.txt
yc serverless function version create \
  --function-name tg-bot \
  --runtime python37-preview \
  --entrypoint main.handler \
  --memory 128m \
  --execution-timeout 3s \
  --source-path pack.zip \
  --service-account-id $SERVICE_ACC \
  --environment TOKEN=$TOKEN \
  --environment FOLDER=$FOLDER
