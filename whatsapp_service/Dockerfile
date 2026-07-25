FROM node:20-slim

RUN apt-get update && \
    apt-get install -y python3 make g++ && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json .
RUN npm install
COPY . .

ENV PORT=3001
EXPOSE 3001
CMD ["node", "index.js"]
