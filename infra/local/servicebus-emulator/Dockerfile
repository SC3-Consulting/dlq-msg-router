FROM mcr.microsoft.com/azure-messaging/servicebus-emulator:latest

# Bake emulator entity configuration into the image to avoid fragile host bind mounts.
COPY config.json /ServiceBus_Emulator/ConfigFiles/Config.json
