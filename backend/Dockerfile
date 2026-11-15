FROM maven:3.9.8-eclipse-temurin-21 AS build
WORKDIR /build
# 使用阿里云镜像源，避免大陆网络访问 Maven Central 失败
COPY settings.xml /root/.m2/settings.xml
COPY pom.xml .
COPY src ./src
RUN mvn -B -DskipTests package

FROM eclipse-temurin:21-jre-alpine
WORKDIR /app
COPY --from=build /build/target/*.jar app.jar
EXPOSE 8000
ENTRYPOINT ["java", "-jar", "app.jar"]
