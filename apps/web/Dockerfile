FROM node:22-alpine
WORKDIR /app
COPY apps/web/package.json apps/web/package-lock.json* ./
RUN npm install
COPY apps/web .
CMD ["npm", "run", "dev", "--", "--hostname", "0.0.0.0"]
