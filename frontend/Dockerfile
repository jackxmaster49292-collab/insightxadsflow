# syntax=docker/dockerfile:1

# --- build ------------------------------------------------------------------
FROM node:22-alpine AS build

WORKDIR /app

# `npm ci` skips devDependencies when NODE_ENV=production. TypeScript and Vite
# both live in devDependencies, so pin it to development for the build stage —
# the compiled output is what ships, not this stage.
ENV NODE_ENV=development

# Copy only the manifests first so the dependency layer is cached and does not
# rebuild every time a source file changes.
COPY package.json package-lock.json ./

# --include=dev is redundant with NODE_ENV above, and deliberately so: either one
# alone is enough to keep the build tools installed.
RUN npm ci --include=dev

# Fail here, with a clear message, rather than several steps later inside a
# toolchain error. This is the check that would have caught the missing
# .dockerignore immediately.
RUN node -e "require.resolve('typescript/lib/tsc.js')" \
    || (echo "typescript is missing from node_modules — check .dockerignore and package-lock.json" && exit 1)

# Source last. .dockerignore keeps node_modules and dist out, so this cannot
# clobber the install above.
COPY . .

RUN npm run build

# --- serve ------------------------------------------------------------------
FROM nginx:1.29-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
