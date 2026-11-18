FROM busybox:1.37
WORKDIR /www
RUN printf 'factory-deployment-ok\n' > index.html
CMD ["httpd", "-f", "-p", "80"]
